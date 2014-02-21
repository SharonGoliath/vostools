#!/usr/bin/env python2.7

"""A FUSE based filesystem view of VOSpace."""

from sys import argv, exit, platform
import time
import vos
import fuse
from fuse import FUSE, Operations, FuseOSError, LoggingMixIn
import tempfile
from threading import Lock
from errno import EACCES, EIO, ENOENT, EISDIR, ENOTDIR, ENOTEMPTY, EPERM, EEXIST, ENODATA, ECONNREFUSED, EAGAIN, ENOTCONN
ENOATTR=93
import os
import io
import vos
from os import O_RDONLY, O_WRONLY, O_RDWR, O_APPEND
import logging
from __version__ import version
from ctypes import cdll
from ctypes.util import find_library
import urlparse 
from CadcCache import Cache, CacheCondition, CacheRetry, CacheAborted, IOProxy
from logExceptions import logExceptions
def flag2mode(flags):
    md = {O_RDONLY: 'r', O_WRONLY: 'w', O_RDWR: 'w+'}
    m = md[flags & (O_RDONLY | O_WRONLY | O_RDWR)]

    if flags | O_APPEND:
        m = m.replace('w', 'a', 1)

    return m

class MyIOProxy(IOProxy):
    def __init__(self, vofs, node):
        super(MyIOProxy, self).__init__()
        self.vofs = vofs
        self.node = node
        # This is the vofile object last used 
        self.lastVOFile = None

    #@logExceptions()
    def writeToBacking(self):
        """ 
        Write a file in the cache to the remote file. 
        """
        logging.debug("PUSHING contents of %s to VOSpace location %s " % 
                (self.cacheFile.cacheDataFile, self.cacheFile.path))

        self.vofs.client.copy(self.cacheFile.cacheDataFile, 
                self.vofs.getNode(self.cacheFile.path).uri)
        node = self.vofs.getNode(self.cacheFile.path, force=True)
        logging.debug("attributes: %s " % str(node.attr ))
        return node.props.get("MD5")

    #@logExceptions()
    def readFromBacking(self, size = None, offset = 0, 
            blockSize = Cache.IO_BLOCK_SIZE):
        """ 
        Read from VOSpace into cache
        """

        # Read a range
        if size is not None or offset != 0:
            if size is None:
                endStr = "" 
            else:
                endStr = str(offset + size - 1)
            range = "bytes=%s-%s" % (str(offset), endStr)
        else:
            range = None
        logging.debug("reading range: %s" % ( str(range)))

        if self.lastVOFile is None:
            logging.debug("Opening a new vo file on %s",
                    self.cacheFile.path)
            self.lastVOFile = self.vofs.client.open(self.cacheFile.path, 
                    mode=os.O_RDONLY, view="data", size=size, range=range)
        else:
            logging.debug("Opening a existing vo file on %s",
                    self.lastVOFile.URLs[self.lastVOFile.urlIndex])
            self.lastVOFile.open(
                    self.lastVOFile.URLs[self.lastVOFile.urlIndex], 
                    bytes=range)
        try:
            logging.debug("reading from %s" % (
                    str(self.lastVOFile.URLs[self.lastVOFile.urlIndex])))
            while True:
                try:
                    buff = self.lastVOFile.read(blockSize)
                except IOError as e:
                    # existing URLs do not work anymore. Try another transfer
                    # negotiation. If it still fails let the error propagate
                    # to client
                    self.lastVOFile = self.vofs.client.open(self.cacheFile.path,
                                                            mode=os.O_RDONLY, 
                                                            view="data", 
                                                            size=size, 
                                                            range=range)
                    buff = self.lastVOFile.read(blockSize)                       

                if buff is None:
                    logging.debug("buffer for %s is None" %
                            (self.cacheFile.path))
                else:
                    logging.debug("Writing: %d bytes at %d to cache for %s" %
                            (len(buff), offset, self.cacheFile.path))
                if not buff:
                    break
                try:
                    self.writeToCache(buff, offset)
                except CacheAborted:
                    # The transfer was aborted.
                    break
                offset += len(buff)
        finally:
            self.lastVOFile.close()
            
        logging.debug("Wrote: %d bytes to cache for %s" % (offset,
                self.cacheFile.path))
 
    def getMD5(self):
        if self.node is None:
            return None
        else:
            return self.node.props.get('MD5')
    
    def getSize(self):
        if self.node is None:
            return None
        else:
            return int(self.node.props.get('length'))


class HandleWrapper(object):
    # A list of all file handles
    handleList = {}
    myLock = Lock()
    """ Wrapper for cache file handlers. Each wrapper represents an open request.
    Multiple wrappers of the same file share the same cache file handler. """
    def __init__(self, cacheFileHandle, readOnly = False):
        self.cacheFileHandle = cacheFileHandle
        self.readOnly = readOnly
        with HandleWrapper.myLock:
            HandleWrapper.handleList[id(self)] = self

    def getId(self):
        return id(self)

    @staticmethod
    def findHandle(id):
        with HandleWrapper.myLock:
            theHandle = HandleWrapper.handleList[id]
        return theHandle

    def release(self):
        with HandleWrapper.myLock:
            del HandleWrapper.handleList[id(self)]


class VOFS(LoggingMixIn, Operations):
#class VOFS(Operations):
    """The VOFS filesystem opperations class.  Requires the vos (VOSpace) python package.

    To use this you will also need a VOSpace account from the CADC.
    """
    ### VOSpace doesn't support these so I've disabled these operations.
    chown = None
    link = None
    mknode = None
    rmdir = None
    symlink = None
    getxattr = None
    listxattr = None
    removexattr = None
    setxattr = None


    def __init__(self, root, cache_dir, options, conn=None, 
            cache_limit=1024, cache_nodes=False):
        """Initialize the VOFS.  
        
        cache_limit is in MB.
        The style here is to use dictionaries to contain information
        about the Node.  The full VOSpace path is used as the Key for
        most of these dictionaries."""

        self.cache_nodes = cache_nodes

        # This dictionary contains the Node data about the VOSpace node in question
        self.node = {}
        # Standard attribtutes of the Node
        # Where in the file system this Node is currently located
        self.loading_dir={}

        # Command line options.
        self.opt = options

        # What is the 'root' of the VOSpace? (eg vos:MyVOSpace) 
        self.root = root

        # VOSpace is a bit slow so we do some cahcing.
        self.cache = Cache(cache_dir, cache_limit, False, 60)
        
        ## All communication with the VOSpace goes through this client connection.
        try:
           self.client = vos.Client(rootNode=root,conn=conn)
        except Exception as e:
           e=FuseOSError(e.errno)
           e.filename=root
           e.strerror=getattr(e,'strerror','failed while making mount')
           raise e

        # Create a condition variable to get rid of those nasty sleeps
        self.condition = CacheCondition(lock=None, timeout=60)


    def __call__(self, op, path, *args):
        return super(VOFS, self).__call__(op, path, *args)

    def __del__(self):
        self.node=None

    def delNode(self,path,force=False):
        """Delete the references associated with this Node"""
        if not self.cache_nodes or force :
            self.node.pop(path,None)

    #@logExceptions()
    def access(self, path, mode):
        """Check if path is accessible.  

        Only checks read access, mode is currently ignored"""
        logging.debug("Checking if -->%s<-- is accessible" %(path))
        try:
            self.getNode(path)
        except:
            return -1
        return 0


    #@logExceptions()
    def chmod(self, path, mode):
        """Set the read/write groups on the VOSpace node based on chmod style modes.

        This function is a bit funny as the VOSpace spec sets the name
        of the read and write groups instead of having mode setting as
        a separate action.  A chmod that adds group permission thus
        becomes a chgrp action.  

        Here I use the logic that the new group will be inherited from
        the container group information.
        """
        logging.debug("Changing mode for %s to %d" % ( path, mode))

        node = self.getNode(path)
        parent = self.getNode(os.path.dirname(path))

        if node.groupread == "NONE":
            node.groupread=parent.groupread
        if node.groupwrite == "NONE":
            node.groupwrite=parent.groupwrite
        # The 'node' object returned by getNode has a chmod method
        # that we now call to set the mod, since we set the group(s)
        # above.  NOTE: If the parrent doesn't have group then NONE is
        # passed up and the groupwrite and groupread will be set tovos:
        # the string NONE.
        if node.chmod(mode):
            # Now set the time of change/modification on the path...
            # TODO: This has to be broken. Attributes may come from Cache if the
            # file is modified. Even if they don't come from the cache, 
            # the getAttr method calls getNode with force=True, which returns 
            # a different Node object than "node". The st_ctime value will be 
            # updated on the newly replaced node in self.node[path] but
            # not in node, then node is pushed to vospace without the st_time
            # change, and then it is pulled back, overwriting the change that
            # was made in self.node[path]. Plus modifying the mtime of the file
            # is not conventional Unix behaviour. The mtime of the parent
            # directory would be changed.
            self.getattr(path)['st_ctime']=time.time()
            ## if node.chmod returns false then no changes were made.
            try:
                self.client.update(node)
                self.getNode(path, force=True)
            except Exception as e:
                logging.debug(str(e))
                logging.debug(type(e))
                e=FuseOSError(getattr(e,'errno',EIO))
                e.filename=path
                e.strerror=getattr(e,'strerror','failed to chmod on %s' %(path))
                raise e

        
    #@logExceptions()
    def create(self, path, flags):
        """Create a node. Currently ignores the ownership mode"""

        logging.debug("Creating a node: %s with flags %s" % (path, str(flags)))

        # Create is handle by the client. 
        # This should fail if the basepath doesn't exist
        try: 
            self.client.open(path,os.O_CREAT).close()
            
            node = self.getNode(path)
            parent=  self.getNode(os.path.dirname(path))

            # Force inheritance of group settings. 
            node.groupread = parent.groupread
            node.groupwrite = parent.groupwrite
            if node.chmod(flags):
                ## chmod returns True if the mode changed but doesn't do update.
                self.client.update(node)
                node = self.getNode(path,force=True)
                
        except Exception as e:
            logging.error(str(e))
            logging.error("Error trying to create Node %s" %(path))
            f=FuseOSError(getattr(e,'errno',EIO))
            f.strerror=getattr(e,'strerror','failed to create %s' %(path))
            raise f

        ## now we can just open the file in the usual way and return the handle
        return self.open(path,os.O_WRONLY)

  
    #@logExceptions()
    def fsync(self,path,datasync,id):
        if self.opt.readonly:
            logging.debug("File system is readonly, no sync allowed")
            return 

        try:
            fh = HandleWrapper.findHandle(id)
        except KeyError:
            raise FuseOSError(EIO)

        if fh.readOnly:
            raise FuseOSError(EPERM)

        fh.cacheFileHandle.fsync()


    def getNode(self,path,force=False,limit=0):
        """Use the client and pull the node from VOSpace.  
        
        Currently force=False is the default... so we never check
        VOSpace to see if the node metadata is different from what we
        have.  This doesn't keep the node metadata current but is
        faster if VOSpace is slow.
        """

      
        logging.debug("force? -> %s path -> %s" % ( force, path))
        ### force if this is a container we've not looked in before
        if path in self.node and not force:
            node = self.node[path]
            if node.isdir() and not limit==0 and not len(node.getNodeList()) > 0 :
               force = True
            if not force:
               logging.debug("Sending back cached metadata for %s" % ( path))
               return node
            

        ## Pull the node meta data from VOSpace.
        try:
            logging.debug("requesting node %s from VOSpace" % ( path))
            self.node[path]=self.client.getNode(path,force=True,limit=limit)
        except Exception as e:
            logging.error(str(e))
            logging.error(type(e))
            ex=FuseOSError(getattr(e,'errno',ENOENT))
            ex.filename=path
            ex.strerror=getattr(e,'strerror','Error getting %s' % (path))
            logging.debug("failing with errno = %d" % ex.errno)
            raise ex

        if self.node[path].isdir() and self.node[path]._nodeList is not None:
            for node in self.node[path]._nodeList:
               subPath=os.path.join(path,node.name)
               self.node[subPath]=node
        return self.node[path]

    #@logExceptions()
    def getattr(self, path, id=None):
        """Build some attributes for this file, we have to make-up some stuff"""

        logging.debug("getting attributes of %s" % ( path))
        # Try to get the attributes from the cache first. This will only return
        # a result if the files has been modified and not flushed to vospace.
        cacheFileAttrs = self.cache.getAttr(path)
        if cacheFileAttrs is not None:
            return cacheFileAttrs

        return self.getNode(path, limit=0, force=False).attr


    #@logExceptions()
    def mkdir(self, path, mode):
        """Create a container node in the VOSpace at the correct location.

        set the mode after creation. """
        #try:
        if 1==1:
            parentNode = self.getNode(os.path.dirname(path), force=False, 
                    limit=1)
            if parentNode and parentNode.props.get('islocked', False):
                logging.info("Parent node of %s is locked." % path)
                raise FuseOSError(EPERM)
            self.client.mkdir(path)
            self.chmod(path,mode)
        #except Exception as e:
        #   logging.error(str(e))
        #   ex=FuseOSError(e.errno)
        #   ex.filename=path
        #   ex.strerror=e.strerror
        #   raise ex
        return

    
    #@logExceptions()
    def open(self, path, flags, *mode):
        """Open file with the desired modes

        Here we return a handle to the cached version of the file
        which is updated if older and out of synce with VOSpace.

        """

        logging.debug("Opening %s with flags %s" % ( path, flag2mode(flags)))
        node = None

        # according to man for open(2), flags must contain one of O_RDWR, 
        # O_WRONLY or O_RDONLY. Since O_RDONLY=0, the only way to detect if 
        # it's a read only is to check whether the other two flags are absent.
        readOnly = ((flags & (os.O_RDWR|os.O_WRONLY)) == 0)
        
        
        cacheFileAttrs = self.cache.getAttr(path)
        if cacheFileAttrs is None:
            # file in the cache not in the process of being modified. 
            # see if this node already exists in the cache; if not get info from vospace 
            try:
                node = self.getNode(path)
            except Exception as e:
                if e.errno == 404:
                    # file does not exist
                    if not flags & os.O_CREAT:
                        # file doesn't exist unless created
                        raise FuseOSError(ENOENT)
                else:
                    raise FuseOSError(e.errno)
            
        ### check if this file is locked, if locked on vospace then don't open
        locked=False

        if node and node.props.get('islocked', False):
             logging.info("%s is locked." % path)
             locked = True


        if not readOnly and node and not locked:
             if node.type == "vos:DataNode":
                  parentNode = self.getNode(os.path.dirname(path),force=False,limit=1)
                  if parentNode.props.get('islocked', False):
                       logging.info("%s is locked by parent node." % path)
                       locked=True
             elif node.type == "vos:LinkNode":
                 try:
                     ## sometimes targetNodes aren't internall... so then not locked
                      targetNode = self.getNode(node.target,force=False,limit=1)  
                      if targetNode.props.get('islocked', False):
                          logging.info("%s target node is locked." % path)
                          locked=True
                      else:
                          targetParentNode = self.getNode(os.path.dirname(node.target),force=False,limit=1)
                          if targetParentNode.props.get('islocked', False):
                              logging.info("%s is locked by target parent node." % path)
                              locked=True
                 except Exception as e:
                      logging.error("Got an error while checking for lock: "+str(e))
                      pass

        if locked and not readOnly:
            # file is locked, cannot write
            e = FuseOSError(ENOENT)
            e.strerror = "Cannot create locked file"
            logging.debug("Cannot create locked file: %s", path)
            raise e
                

        
        myProxy = MyIOProxy(self, node)
        # new file in cache library if no node information (node not in 
        # vospace) or file is open for truncate
        isNew = ((cacheFileAttrs == None) and (node == None) or 
                ((flags & os.O_TRUNC) != 0))
        
        return HandleWrapper(self.cache.open(path, isNew, myProxy), 
                readOnly).getId()

    #@logExceptions()
    def read(self, path, size=0, offset=0, id=None):
        """ Read the required bytes from the file and return a buffer containing
        the bytes.
        """


        ## Read from the requested filehandle, which was set during 'open'
        if id is None:
            raise FuseOSError(EIO)

        logging.debug("reading range: %s %d %d %d" % ( path, size, offset, id))

        try:
            fh = HandleWrapper.findHandle(id)
        except KeyError:
            raise FuseOSError(EIO)

        try:
            return fh.cacheFileHandle.read(size, offset)
        except CacheRetry:
            e = FuseOSError(EAGAIN)
            e.strerror = "Timeout waiting for file read"
            logging.debug("Timeout Waiting for file read: %s", path)
            raise e

        
    #@logExceptions()
    def readlink(self, path):
        """Return a string representing the path to which the symbolic link points.

	path: filesystem location that is a link

	returns the file that path is a link to. 

	Currently doesn't provide correct capabilty for VOSpace FS.
        """
        return self.getNode(path).target
        

    #@logExceptions()
    def readdir(self, path, id):
        """Send a list of entries in this directory"""
        logging.debug("Getting directory list for %s " % ( path))
        ## reading from VOSpace can be slow, we'll do this in a thread
        import thread, time
        with self.condition:
            if not self.loading_dir.get(path,False):
                self.loading_dir[path]=True
                thread.start_new_thread(self.load_dir,(path, ))

            while self.loading_dir.get(path,False):
                logging.debug("Waiting ... ")
                try: 
                    self.condition.wait()
                except CacheRetry:
                    e = FuseOSError(EAGAIN)
                    e.strerror = "Timeout waiting for directory listing"
                    logging.debug("Timeout Waiting for directory read: %s", path)
                    raise e
        return ['.','..'] + [e.name.encode('utf-8') for e in self.getNode(
                path,force=False,limit=None).getNodeList() ]

    #@logExceptions()
    def load_dir(self,path):
        """Load the dirlist from VOSpace. 
        This should always be run in a thread."""
        try:
            logging.debug("Starting getNodeList thread")
            self.getNode(path,force=not self.opt.cache_nodes,
                    limit=None).getNodeList()
            logging.debug("Got listing for %s" %(path))
        finally:
            self.loading_dir[path] = False
            with self.condition:
                self.condition.notify_all()
        return 

    #@logExceptions()
    def release(self, path, id):
        """Close the file"""
        logging.debug("releasing file %d " % id)
        try:
            fh = HandleWrapper.findHandle(id)
        except KeyError:
            raise FuseOSError(EIO)

        try:
            fh.cacheFileHandle.release()
            fh.release()
            if ((fh.cacheFileHandle.fileModified) and 
                    (fh.cacheFileHandle.path in self.node)):
                #local copy modified. remove from list
                logging.debug("deleting old node %s " % path)
                del self.node[fh.cacheFileHandle.path]
        except CacheRetry, e:
            #it's taking too long. Notify FUSE
            e = FuseOSError(EAGAIN)
            e.strerror = "Timeout waiting for file release"
            logging.debug("Timeout Waiting for file release: %s",
                    fh.cacheFileHandle.path)
            raise e
        except Exception, e:
            #unexpected problem
            raise FuseOSError(EIO)
        return 


    #@logExceptions()
    def rename(self,src,dest):
        """Rename a data node into a new container"""
        logging.debug("Original %s -> %s" % ( src,dest))
        #if not self.client.isfile(src):
        #   return -1
        #if not self.client.isdir(os.path.dirname(dest)):
        #    return -1
        try:
            logging.debug("Moving %s to %s" % ( src,dest))
            result=self.client.move(src,dest)
            logging.debug(str(result))
            if result:
                self.cache.renameFile(src, dest)
                return 0
            return -1
        except Exception, e:
            logging.error("%s" % str(e))
            import re
            if re.search('NodeLocked', str(e)) != None:
                raise FuseOSError(EPERM)
            return -1
    

    #@logExceptions()
    def rmdir(self,path):
        node=self.getNode(path)
        #if not node.isdir():
        #    raise FuseOSError(ENOTDIR)
        #if len(node.getNodeList())>0:
        #    raise FuseOSError(ENOTEMPTY)
        if node and node.props.get('islocked', False):
            logging.info("%s is locked." % path)
            raise FuseOSError(EPERM)
        self.client.delete(path)
        self.delNode(path,force=True)

        
    #@logExceptions()
    def statfs(self, path):
        node=self.getNode(path)
        block_size=512
        bytes=2**33
        free=2**33
        
        if 'quota' in node.props:
            bytes=int(node.props.get('quota',2**33))
            used=int(node.props.get('length',2**33))
            free=bytes-used
        sfs={}
        sfs['f_bsize']=block_size
        sfs['f_frsize']=block_size
        sfs['f_blocks']=int(bytes/block_size)
        sfs['f_bfree']=int(free/block_size)
        sfs['f_bavail']=int(free/block_size)
        sfs['f_files']=len(node.getNodeList())
        sfs['f_ffree']=2*10
        sfs['f_favail']=2*10
        sfs['f_flags']=0
        sfs['f_namemax']=256
        return sfs
            
    
    #@logExceptions()
    def truncate(self, path, length, id=None):
        """Perform a file truncation to length bytes"""
        logging.debug("Attempting to truncate %s : %s (%d)" % ( path, id,length))

        if id is None:
            # Ensure the file exists.
            if length == 0:
                try:
                    fh = self.open(path, os.O_RDWR | os.O_TRUNC)
                finally:
                    self.release(path, fh)
                return

            fh = self.open(path, os.O_RDWR)
            try:
                self.truncate(path, length, fh)
            except Exception:
                raise
            finally:
                self.release(path, fh)
        else:
            try:
                fh = HandleWrapper.findHandle(id)
            except KeyError:
                raise FuseOSError(EIO)
            if fh.readOnly:
                logging.debug("file was not opened for writing")
                raise FuseOSError(EPERM)
            fh.cacheFileHandle.truncate(length)
        return

    #@logExceptions()
    def unlink(self,path):
    	node = self.getNode(path, force=False, limit=1)
        if node and node.props.get('islocked', False):
            logging.info("%s is locked." % path)
            raise FuseOSError(EPERM)
        self.cache.unlinkFile(path)
        if node:
            self.client.delete(path)
        self.delNode(path,force=True)


    #@logExceptions()
    def write(self, path, data, size, offset, id=None):
        import ctypes

        if self.opt.readonly:
            logging.debug("File system is readonly.. so writing 0 bytes\n")
            return 0

        try:
            fh = HandleWrapper.findHandle(id)
        except KeyError:
            raise FuseOSError(EIO)

        if fh.readOnly:
            logging.debug("file was not opened for writing")
            raise FuseOSError(EPERM)

        logging.debug("%s -> %s" % ( path,fh))
        logging.debug("%d --> %d" % ( offset, offset+size))

        try:
            return fh.cacheFileHandle.write(data, size, offset)
        except CacheRetry:
            e = FuseOSError(EAGAIN)
            e.strerror = "Timeout waiting for file write"
            logging.debug("Timeout Waiting for file write: %s", path)
            raise e
        
        
