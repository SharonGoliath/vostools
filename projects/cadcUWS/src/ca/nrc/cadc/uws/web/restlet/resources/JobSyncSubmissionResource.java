/*
************************************************************************
*******************  CANADIAN ASTRONOMY DATA CENTRE  *******************
**************  CENTRE CANADIEN DE DONNÉES ASTRONOMIQUES  **************
*
*  (c) 2009.                            (c) 2009.
*  Government of Canada                 Gouvernement du Canada
*  National Research Council            Conseil national de recherches
*  Ottawa, Canada, K1A 0R6              Ottawa, Canada, K1A 0R6
*  All rights reserved                  Tous droits réservés
*
*  NRC disclaims any warranties,        Le CNRC dénie toute garantie
*  expressed, implied, or               énoncée, implicite ou légale,
*  statutory, of any kind with          de quelque nature que ce
*  respect to the software,             soit, concernant le logiciel,
*  including without limitation         y compris sans restriction
*  any warranty of merchantability      toute garantie de valeur
*  or fitness for a particular          marchande ou de pertinence
*  purpose. NRC shall not be            pour un usage particulier.
*  liable in any event for any          Le CNRC ne pourra en aucun cas
*  damages, whether direct or           être tenu responsable de tout
*  indirect, special or general,        dommage, direct ou indirect,
*  consequential or incidental,         particulier ou général,
*  arising from the use of the          accessoire ou fortuit, résultant
*  software.  Neither the name          de l'utilisation du logiciel. Ni
*  of the National Research             le nom du Conseil National de
*  Council of Canada nor the            Recherches du Canada ni les noms
*  names of its contributors may        de ses  participants ne peuvent
*  be used to endorse or promote        être utilisés pour approuver ou
*  products derived from this           promouvoir les produits dérivés
*  software without specific prior      de ce logiciel sans autorisation
*  written permission.                  préalable et particulière
*                                       par écrit.
*
*  This file is part of the             Ce fichier fait partie du projet
*  OpenCADC project.                    OpenCADC.
*
*  OpenCADC is free software:           OpenCADC est un logiciel libre ;
*  you can redistribute it and/or       vous pouvez le redistribuer ou le
*  modify it under the terms of         modifier suivant les termes de
*  the GNU Affero General Public        la “GNU Affero General Public
*  License as published by the          License” telle que publiée
*  Free Software Foundation,            par la Free Software Foundation
*  either version 3 of the              : soit la version 3 de cette
*  License, or (at your option)         licence, soit (à votre gré)
*  any later version.                   toute version ultérieure.
*
*  OpenCADC is distributed in the       OpenCADC est distribué
*  hope that it will be useful,         dans l’espoir qu’il vous
*  but WITHOUT ANY WARRANTY;            sera utile, mais SANS AUCUNE
*  without even the implied             GARANTIE : sans même la garantie
*  warranty of MERCHANTABILITY          implicite de COMMERCIALISABILITÉ
*  or FITNESS FOR A PARTICULAR          ni d’ADÉQUATION À UN OBJECTIF
*  PURPOSE.  See the GNU Affero         PARTICULIER. Consultez la Licence
*  General Public License for           Générale Publique GNU Affero
*  more details.                        pour plus de détails.
*
*  You should have received             Vous devriez avoir reçu une
*  a copy of the GNU Affero             copie de la Licence Générale
*  General Public License along         Publique GNU Affero avec
*  with OpenCADC.  If not, see          OpenCADC ; si ce n’est
*  <http://www.gnu.org/licenses/>.      pas le cas, consultez :
*                                       <http://www.gnu.org/licenses/>.
*
*  $Revision: 4 $
*
************************************************************************
*/

package ca.nrc.cadc.uws.web.restlet.resources;

import ca.nrc.cadc.uws.ExecutionPhase;
import ca.nrc.cadc.uws.InvalidServiceException;
import ca.nrc.cadc.uws.SyncJobRunner;
import ca.nrc.cadc.uws.util.BeanUtil;
import ca.nrc.cadc.uws.util.StringUtil;
import ca.nrc.cadc.uws.web.restlet.representation.SyncRepresentation;
import org.restlet.representation.Representation;
import org.restlet.resource.Get;
import org.apache.log4j.Logger;

import java.io.IOException;
import java.net.URL;

import org.jdom.Document;
import org.restlet.data.MediaType;
import org.restlet.data.Status;
import org.restlet.representation.EmptyRepresentation;

/**
 * Synchronous Job submission, and result retrieval.  This Resource will submit
 * a job upon a GET, then return either the Result of the Job submission, or
 * an error if it did not complete properly.
 * @deprecated not possible to set the MediaType correctly with SyncRepresentation
 * @see ca.nrc.cadc.uws.server.SyncServlet
 */
public class JobSyncSubmissionResource extends BaseJobResource
{
    private static final Logger LOGGER =
            Logger.getLogger(JobSyncSubmissionResource.class);
    protected static final long POLLING_TIME_INTERVAL_MILLIS = 1000l;
    protected static final long MAX_POLLING_TIME_MILLIS = 180000l;

    /**
     * Obtain the XML Representation of this Request.
     *
     * @return The XML Representation, fully populated.
     */
    @Get
    @Override
    public Representation represent()
    {
        // If Job isn't in to QUEUED state, return a 404.
        if (!job.getExecutionPhase().equals(ExecutionPhase.PENDING))
        {
            setStatus(Status.CLIENT_ERROR_NOT_FOUND);
            return new EmptyRepresentation();
        }

        SyncJobRunner jobRunner = createJobRunner();
        jobRunner.setJob(job);
        jobRunner.setJobManager(getJobManager());

        job.setExecutionPhase(ExecutionPhase.QUEUED);
        job = getJobManager().persist(job);

        // BUG: this method should be called with the correct Subject
        URL redirectURL = jobRunner.getRedirectURL();
        if (redirectURL != null)
        {
            redirectSeeOther(redirectURL.toString());
            return new EmptyRepresentation();
        }

        // streaming output
        // BUG: this method method should be called with correct Subject and it is
        // fundamentally flawed since it cannot set the MediaType because the JobRunner
        // cannot really predict it
        return new SyncRepresentation(null, job, getJobManager(), jobRunner);
    }

    /**
     * Assemble the XML for this Resource's Representation into the given
     * Document.
     *
     * @param document The Document to build up.
     * @throws java.io.IOException If something went wrong or the XML cannot be
     *                             built.
     */
    protected void buildXML(final Document document) throws IOException
    {
        // Do Nothing.
    }

    /**
     * Obtain a new instance of the Job Runner interface as defined in the
     * Context
     *
     * @return  The JobRunner instance.
     */
    @SuppressWarnings("unchecked")
    protected SyncJobRunner createJobRunner()
    {
        String jobRunnerClassName =
                getContext().getParameters().getFirstValue(BeanUtil.UWS_RUNNER);
        if (!StringUtil.hasText(jobRunnerClassName))
        {
            throw new InvalidServiceException(
                    "The JobRunner is mandatory!\n\n Please set the "
                    + BeanUtil.UWS_RUNNER + " context-param in the web.xml, "
                    + "or insert it into the Context manually.");
        }
        final BeanUtil beanUtil = new BeanUtil(jobRunnerClassName);
        return (SyncJobRunner) beanUtil.createBean();
    }
    
    /**
     * Execute the current Job.  This method will set a new Job Runner with
     * every execution to make it ThreadSafe.
     */
    /*
    protected void executeJob(SyncJobRunner jobRunner)
    {
        if (jobIsActive())
            return;

        if (jobIsPending())
        {
            job.setExecutionPhase(ExecutionPhase.QUEUED);
            this.job = getJobManager().persist(job);

            if (job.getOwner() == null)
            {
                Runnable r = new TimeTrackingRunnable(getJobManager(), jobRunner);
                r.run();
            }
            else
            {
            	Subject.doAs(job.getOwner(), new PrivilegedActionJobRunner(getJobManager(), jobRunner));
            }
        }
        // get current state
        this.job = getJobManager().getJob(job.getID());
    }
     *
     */

    /**
     * Poll the current job.
     */
    /*
    protected void pollRunningJob()
    {
        pollRunningJob(MAX_POLLING_TIME_MILLIS);
    }
     *
     */

    /**
     * Poll the current job and wait for it to complete within the given amount
     * of time.
     *
     * @param maxWait       Maximum time to poll for in milliseconds.
     */
    /*
    protected void pollRunningJob(final long maxWait)
    {
        long waitTime = maxWait;
        try
        {
            LOGGER.debug("pollRunningJob: START");
            while (jobIsActive() && (waitTime > 0l))
            {
                LOGGER.debug("pollRunningJob: sleeping for " + POLLING_TIME_INTERVAL_MILLIS);
                Thread.sleep(POLLING_TIME_INTERVAL_MILLIS);
                this.job = getJobManager().getJob(job.getID());
                waitTime = waitTime - POLLING_TIME_INTERVAL_MILLIS;
            }
            LOGGER.debug("pollRunningJob: DONE");
        }
        catch(InterruptedException iex)
        {
            LOGGER.warn("pollRunningJob: interrupted");
        }
        catch(Throwable t)
        {
            LOGGER.error("pollRunningJob: FAILED", t);
        }
    } 
     * 
     */

}
