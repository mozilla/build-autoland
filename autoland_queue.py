import time
import os, sys
import re
import logging
import logging.handlers
import datetime
import urllib2

from utils import mq_utils, bz_utils, ldap_utils, common
base_dir = common.get_base_dir(__file__)
import site
site.addsitedir('%s/../../lib/python' % (base_dir))

from utils.db_handler import DBHandler, PatchSet, Branch, Comment

log = logging.getLogger()
LOGFORMAT = logging.Formatter(
        '%(asctime)s\t%(module)s\t%(funcName)s\t%(message)s')
LOGFILE = os.path.join(base_dir, 'autoland_queue.log')
LOGHANDLER = logging.handlers.RotatingFileHandler(LOGFILE,
                    maxBytes=50000, backupCount=5)

config = common.get_configuration(os.path.join(base_dir, 'config.ini'))
ldap = ldap_utils.ldap_util(config['ldap_host'], int(config['ldap_port']),
        config['ldap_bind_dn'], config['ldap_password'])
bz = bz_utils.bz_util(api_url=config['bz_api_url'], url=config['bz_url'],
        attachment_url=config['bz_attachment_url'],
        username=config['bz_username'], password=config['bz_password'])
mq = mq_utils.mq_util()
db = DBHandler(config['databases_autoland_db_url'])

if config.get('staging', False):
    import subprocess

def get_first_autoland_tag(whiteboard):
    """
    Returns the first autoland tag in the whiteboard
    """
    r = re.compile(r'\[autoland(-[^\[\]:]+)?((:\d+(,\d+)*)'
                   r'|(:-[^\[\]:]+)){0,2}\]', re.I)
    s = r.search(whiteboard)
    if s != None:
        s = s.group().lower()
    return s

def get_branch_from_tag(tag):
    """
    Returns a list of branch names from the given autoland tag.
    All tags must include "-branch" including try.
    eg. [autoland-try], [autoland-mozilla-central],
        [autoland-mozilla-aurora,mozilla-beta]
    """
    r = re.compile('\[autoland-([^:\]]+)', re.I)
    s = r.search(tag)
    if s == None:
        return None
    return re.split(',', s.groups()[0].lower())

def get_try_syntax_from_tag(tag):
    # return a string of try_syntax (must start with -)
    parts = tag.strip('[]').split(':')
    for part in parts:
        if part.startswith('-'):
            return part

def get_patches_from_tag(tag):
    # return a string of comma-delimited digits that represent attachment IDs
    patches = ''
    parts = tag.strip('[]').split(':')
    r = re.compile('^[0-9]+(,^[0-9]+)*', re.I)
    for part in parts:
        s = r.search(part.strip())
        if s != None:
            values = part.strip().split(',')
            for v in tuple(values):
                try:
                    int(v)
                except:
                    # well it's not valid then, don't include it
                    values.remove(v)
                    pass
            patches = ','.join(values)
    return patches

def get_reviews(attachment):
    """
    Takes attachment JSON, returns a list of reviews.
    Each review (in the list) is a dictionary containing:
        - Review type (review, superreview, ui-review)
        - Reviewer
        - Review Result (+, -, ?)
    """
    reviews = []
    if not 'flags' in attachment:
        return reviews
    for flag in attachment['flags']:
        for review_type in ('review', 'superreview', 'ui-review'):
            if flag.get('name') == review_type:
                reviews.append({
                        'type':review_type,
                        'reviewer':bz.get_user_info(flag['setter']['name']),
                        'result':flag['status']
                        })
                break
    return reviews

def get_approvals(attachment):
    """
    Takes attachment JSON, returns a list of approvals.
    Each approval (in the list) is a dictionary containing:
        - Approval type
        - Approver
        - Approval Result (+, -, ?)
    """
    print "Checking attachment"
    print attachment
    approvals = []
    app_re = re.compile(r'approval-')
    if not 'flags' in attachment:
        print "no flags"
        return approvals
    for flag in attachment['flags']:
        print "Flag: %s" % (flag)
        if app_re.match(flag.get('name')):
            approvals.append({
                    'type': app_re.sub('', flag.get('name')),
                    'approver':bz.get_user_info(flag['setter']['name']),
                    'result':flag['status']
                    })
    return approvals

def get_approval_status(patches, branch, perms):
    """
    Returns the approval status of the patchset for the given branch.
    Ensures that any passed approvals are also VALID approvals
        * The approval was given by someone with correct permission level
    If any patches failed approval, returns
        ('FAIL', [failed_patches])
    If any patches have invalid approval, returns
        ('INVALID', [invalid_patches])
    If any patches are still a? or have no approval flags,
        returns ('PENDING', [pending_patches])
    If all patches have at least one, and only passing approvals,
        returns ('PASS',)
    """
    if len(patches) == 0:
        return ('FAIL', None)
    failed = []
    invalid = []
    pending = []
    for patch in patches:
        approved = False
        p_id = patch['id']
        for app in patch['approvals']:
            if app['type'].strip().lower() != branch:
                continue
            if app['result'] == '+':
                # Found an approval, but keep on looking in case there is
                # afailed or pending approval.
                if common.in_ldap_group(ldap, app['approver']['email'], perms):
                    log.info("PERMISSIONS: Approver %s has valid %s "
                             "permissions for branch %s"
                             % (app['approver']['email'], perms, branch))
                    approved = True
                else:
                    if p_id not in invalid: invalid.append(str(p_id))
            elif app['result'] == '?':
                if p_id not in pending: pending.append(str(p_id))
            else:
                # non-approval
                if p_id not in failed: failed.append(str(p_id))
        if not approved:
            # There is no approval, so consider it pending.
            if p_id not in pending: pending.append(str(p_id))

    if failed:
        return ('FAIL', failed)
    if invalid:
        return ('INVALID', invalid)
    if pending:
        return ('PENDING', pending)
    return ('PASS',)

def get_review_status(patches, perms):
    """
    Returns the review status of the patchset.
    Ensures that any passed reviews are also VALID reviews
        * The review was done by someone with the correct permission level
    If any patches failed review, returns
        ('FAIL', [failed_patches])
    If any patches have invalid review, returns
        ('INVALID', [invalid_patches])
    If any patches are still r? or have no review flags,
        returns ('PENDING', [pending_patches])
    If all patches have at least one, and only passing reviews,
        returns ('PASS',)
    """
    if len(patches) == 0:
        return ('FAIL', None)
    failed = []
    invalid = []
    pending = []
    for patch in patches:
        reviewed = False
        p_id = patch['id']
        for rev in patch['reviews']:
            if rev['result'] == '+':
                # Found a passed review, but keep on looking in case there is
                # a failed or pending review.
                if common.in_ldap_group(ldap, rev['reviewer']['email'], perms):
                    log.info("PERMISSIONS: Reviewer %s has valid %s "
                             "permissions" % (rev['reviewer']['email'], perms))
                    reviewed = True
                else:
                    if p_id not in invalid: invalid.append(str(p_id))
            elif rev['result'] == '?':
                if p_id not in pending: pending.append(str(p_id))
            else:
                # non-approval
                if p_id not in failed: failed.append(str(p_id))
        if not reviewed:
            # There is no review on this, so consider it to be pending.
            if p_id not in pending: pending.append(str(p_id))

    if failed:
        return ('FAIL', failed)
    if invalid:
        return ('INVALID', invalid)
    if pending:
        return ('PENDING', pending)
    return ('PASS',)

def get_patchset(bug_id, user_patches=None):
    """
    If user_patches specified, only fetch the information on those specific
    patches from the bug.
    If user_patches not specified, fetch the information on all patches from
    the bug.

    The returned patchset will return ALL patches, reviews, and approvals.

    Return value is of the JSON structure:
        [
            { 'id' : 54321,
              'author' : { 'name' : 'Name',
                           'email' : 'me@email.com' },
              'reviews' : [
                    { 'reviewer' : 'email',
                      'type' : 'superreview',
                      'result' : '+'
                    },
                    { ... }
                ],
              'approvals' : [
                    { 'approver' : 'email',
                      'type' : 'mozilla-beta',
                      'result' : '+'
                    },
                    { ... }
                ]
            },
            { ... }
        ]
    """
    patchset = []   # hold the final patchset information

    # grab the bug data
    bug_data = bz.request('bug/%s' % (bug_id))
    if 'attachments' not in bug_data:
        return None     # bad bug id, or no attachments

    if user_patches:
        # user-specified patches, need to pull them in that set order
        user_patches = list(user_patches)    # take a local copy, passed by ref
        for user_patch in tuple(user_patches):
            for attachment in bug_data['attachments']:
                if attachment['id'] != user_patch \
                        or not attachment['is_patch'] \
                        or attachment['is_obsolete']:
                    continue
                patch = { 'id' : user_patch,
                          'author' : bz.get_user_info(
                              attachment['attacher']['name']),
                          'approvals' : get_approvals(attachment),
                          'reviews' : get_reviews(attachment) }
                patchset.append(patch)
                # remove the patch from user_patches to check all listed
                # patches were pulled
                user_patches.remove(patch['id'])
        if len(user_patches) != 0:
            # not all requested patches could be picked up
            # XXX TODO - should we still push what patches _did get picked up?
            log.debug('Autoland failure. Not all user_patches could '
                      'be picked up from bug.')
            post_comment(('Autoland Failure\nSpecified patches %s '
                          'do not exist, or are not posted to this bug.'
                          % (user_patches)), bug_id)
            return None
    else:
        # no user-specified patches, grab them in the order they were posted.
        for attachment in bug_data['attachments']:
            if not attachment['is_patch'] or attachment['is_obsolete']:
                # not a valid patch to be pulled
                continue
            patch = { 'id' : attachment['id'],
                      'author' : bz.get_user_info(
                          attachment['attacher']['name']),
                      'approvals' : get_reviews(attachment),
                      'reviews' : get_approvals(attachment) }
            patchset.append(patch)

    if len(patchset) == 0:
        post_comment('Autoland Failure\n There are no patches to run.', bug_id)
        patchset = None

    return patchset

def bz_search_handler():
    """
    Search bugzilla whiteboards for Autoland jobs.
    Search handler, for the moment, only supports push to try,
    and then to branch. It cannot push directly to branch.
    """
    bugs = []
    try:
        bugs = bz.get_matching_bugs('whiteboard', '\[autoland.*\]')
    except (urllib2.HTTPError, urllib2.URLError), e:
        log.error("Error while polling bugzilla: %s" % (e))
        return
    if not bugs:
        return

    for (bug_id, whiteboard) in bugs:
        tag = get_first_autoland_tag(whiteboard)
        #log.debug('Bug %s with tag %s' % (bug_id, tag))

        if tag == None or re.search('in-queue', tag) != None:
            # Strange that it showed up if None
            continue

        # get the branches
        branches = get_branch_from_tag(tag)
        log.debug('Flagged for landing on branches: %s' % (branches))
        if not branches:
            # this was probably flagged [autoland], since it was picked up
            # and doesn't have a branch attached.
            log.debug('No branches from tag %s' % (tag))
            continue
        for branch in tuple(branches):
            # clean out any invalid branch names
            # job will still land to any correct branches
            if db.BranchQuery(Branch(name=branch)) == None:
                branches.remove(branch)
                log.error('Branch %s does not exist.' % (branch))

        # If there are no correct or permissive branches, go to next bug
        if not branches:
            continue

        log.debug('Found and processing tag %s' % (tag))
        # get the explicitly listed patches, if any
        patch_group = get_patches_from_tag(tag) if not None else []

        # get try syntax, if any
        try_syntax = get_try_syntax_from_tag(tag)

        ps = PatchSet()
        ps.try_syntax = try_syntax
        ps.patches = patch_group
        ps.bug_id = bug_id

        # check patch reviews & permissions
        patches = get_patchset(ps.bug_id, ps.patchList())
        if not patches:
            # do not have patches to push, kick it out of the queue
            bz.remove_whiteboard_tag(tag.replace('[', '\[').replace(']', '\]'),
                    bug_id)
            log.error('No valid patches attached, nothing for '
                      'Autoland to do here, removing this bug from the queue.')
            continue
        ps.author = patches[0]['author']['email']
        ps.patches = ','.join(str(x['id']) for x in patches)

        # get the branches
        branches = get_branch_from_tag(tag)
        print "Getting branches: %s" % branches
        comment = []
        for branch in tuple(branches):
            # clean out any invalid branch names
            # job will still land to any correct branches
            db_branch = db.BranchQuery(Branch(name=branch))
            if db_branch == None:
                branches.remove(branch)
                log.error('Branch %s does not exist.' % (branch))
                continue
            db_branch = db_branch[0]

            branch_perms = ldap.get_branch_permissions(branch)

            # check if branch landing r+'s are present
            # check branch name against try since branch on try iteration
            # will also have try_run set to True
            if branch.lower() != 'try':
                r_status = get_review_status(patches, branch_perms)
                if r_status[0] == 'FAIL':
                    comment.append('Review failed on patch(es): %s'
                                    % (' '.join(r_status[1])))
                    branches.remove(branch)
                    continue
                elif r_status[0] == 'PENDING':
                    comment.append('Review not yet given on patch(es): %s'
                                    % (' '.join(r_status[1])))
                    branches.remove(branch)
                    continue
                elif r_status[0] == 'INVALID':
                    comment.append('Reviewer doesn\'t have correct '
                                   'permissions for %s on patch(es): %s'
                                    % (branch, ' '.join(r_status[1])))
                    branches.remove(branch)
                    continue

            # check if approval granted on branch push.
            if db_branch.approval_required:
                a_status = get_approval_status(patches, branch, branch_perms)
                if a_status[0] == 'FAIL':
                    comment.append('Approval failed on patch(es): %s'
                                    % (' '.join(r_status[1])))
                    branches.remove(branch)
                    continue
                elif a_status[0] == 'PENDING':
                    comment.append('Approval not yet given for branch %s'
                                   'on patch(es): %s'
                                    % (' '.join(r_status[1])))
                    branches.remove(branch)
                    continue
                elif a_status[0] == 'INVALID':
                    comment.append('Approver for branch %s '
                                   'doesn\'t have correct '
                                   'permissions on patch(es): %s'
                                    % (' '.join(r_status[1])))
                    branches.remove(branch)
                    continue

            # add the one branch to the database for landing
            job_ps = ps
            job_ps.branch = branch
            if db.PatchSetQuery(ps) != None:
                # we already have this in the db, don't run this branch
                comment.append('Already landing patches %s on branch %s.'
                                % (job_ps.patches, branch))
                branches.remove(branch)
                log.debug('Duplicate patchset, removing branch.')
                continue

            # all runs will get a try_run by default for now
            # if it has a different branch listed, then it will do try run
            # then go to branch
            # add try_run attribute here so that PatchSetQuery will match
            # patchsets in any stage of their lifecycle
            job_ps.try_run = 1
            log.info('Inserting job: %s' % (job_ps))
            patchset_id = db.PatchSetInsert(job_ps)
            log.info('Insert Patchset ID: %s' % (patchset_id))

        if not branches:
            comment.insert(0, 'Autoland Failure:')
        elif branches and comment:
            comment.insert(0, 'Autoland Warning: '
                              'Only landing on branch(es): %s'
                               % (' '.join(branches)))

        post_comment('\n\t'.join(comment), bug_id)

        if not branches:
            # remove whiteboard tag
            bz.remove_whiteboard_tag('\[autoland[^\[\]]*\]', bug_id)
        else:
            bz.replace_whiteboard_tag('\[autoland[^\[\]]*\]',
                    '[autoland-in-queue]', bug_id)

@mq.generate_callback
def message_handler(message):
    """
    Handles json messages received. Expected structures are as follows:
    For a JOB:
        {
            'type' : 'JOB',
            'bug_id' : 12345,
            'branch' : 'mozilla-central',
            'try_run' : 1,
            'patches' : [ 53432, 64512 ],
        }
    For a SUCCESS/FAILURE:
        {
            'type' : 'ERROR',
            'action' : 'PATCHSET.APPLY',
            'patchsetid' : 123,
        }
    For try run PASS/FAIL:
        {
            'type' : 'SUCCESS',
            'action' : 'TRY.RUN',
            'revision' : '8dc05498d708',
        }
    """
    msg = message['payload']
    log.info('Received message:\n%s' % (message))
    if not 'type' in msg:
        log.error('Got bad mq message: %s' % (msg))
        return
    if msg['type'] == 'JOB':
        if 'try_run' not in msg:
            msg['try_run'] = 1
        if 'bug_id' not in msg:
            log.error('Bug ID not specified.')
            return
        if 'branches' not in msg:
            log.error('Branches not specified.')
            return
        if 'patches' not in msg:
            log.error('Patch list not specified')
            return
        if msg['try_run'] == 0:
            # XXX: Nothing to do, don't add.
            log.error('ERROR: try_run not specified.')
            return

        if msg['branches'].lower() == ['try']:
            msg['branches'] = ['mozilla-central']
            msg['try_run'] = 1

        ps = PatchSet(bug_id=msg.get('bug_id'),
                      branch=msg.get('branch'),
                      try_run=msg.get('try_run'),
                      try_syntax=msg.get('try_syntax'),
                      patches=msg.get('patches')
                     )
        patchset_id = db.PatchSetInsert(ps)
        log.info('Insert PatchSet ID: %s' % (patchset_id))

    # attempt comment posting immediately, no matter the message type
    comment = msg.get('comment')
    if comment:
        # Handle the posting of a comment
        bug_id = msg.get('bug_id')
        if not bug_id:
            log.error('Have comment, but no bug_id')
        else:
            post_comment(comment, bug_id)

    if msg['type'] == 'SUCCESS':
        if msg['action'] == 'TRY.PUSH':
            # Successful push, add corresponding revision to patchset
            ps = db.PatchSetQuery(PatchSet(id=msg['patchsetid']))
            if ps == None:
                log.info('No corresponding patch set found for %s'
                        % (msg['patchsetid']))
                return
            ps = ps[0]
            log.debug('Got patchset back from DB: %s' % (ps))
            ps.revision = msg['revision']
            db.PatchSetUpdate(ps)
            log.debug('Added revision %s to patchset %s'
                    % (ps.revision, ps.id))

        elif '.RUN' in msg['action']:
            # this is a result from schedulerDBpoller
            ps = db.PatchSetQuery(PatchSet(revision=msg['revision']))
            if ps == None:
                log.error('Revision %s not found in database.'
                        % (msg['revision']))
                return
            ps = ps[0]
            # is this the try run before push to branch?
            if ps.try_run and \
                    msg['action'] == 'TRY.RUN' and ps.branch != 'try':
                # remove try_run, when it comes up in the queue
                # it will trigger push to branch(es)
                ps.try_run = 0
                ps.push_time = None
                log.debug('Flag patchset %s revision %s for push to branch.'
                        % (ps.id, ps.revision))
                db.PatchSetUpdate(ps)
            else:
                # close it!
                bz.remove_whiteboard_tag('\[autoland-in-queue\]', ps.bug_id)
                db.PatchSetDelete(ps)
                log.debug('Deleting patchset %s' % (ps.id))
                return

        elif msg['action'] == 'BRANCH.PUSH':
            # Guaranteed patchset EOL
            ps = db.PatchSetQuery(PatchSet(id=msg['patchsetid']))[0]
            bz.remove_whiteboard_tag('\[autoland-in-queue\]', ps.bug_id)
            db.PatchSetDelete(ps)
            log.debug('Successful push to branch of patchset %s.' % (ps.id))
    elif msg['type'] == 'TIMED_OUT':
        ps = None
        if msg['action'] == 'TRY.RUN':
            ps = db.PatchSetQuery(PatchSet(revision=msg['revision']))
            if ps == None:
                log.error('No corresponding patchset found for '
                          'timed out revision %s' % (msg['revision']))
                return
            ps = ps[0]
        if ps:
            # remove it from the queue, timeout should've been comented to bug
            # XXX: (shall we confirm that here with bz_utils.has_comment?)
            bz.remove_whiteboard_tag('\[autoland-in-queue\]', ps.bug_id)
            db.PatchSetDelete(ps)
            log.debug('Received time out on %s, deleting patchset %s'
                    % (msg['action'], ps.id))
    elif msg['type'] == 'ERROR' or msg['type'] == 'FAILURE':
        ps = None
        if msg['action'] == 'TRY.RUN' or msg['action'] == 'BRANCH.RUN':
            ps = db.PatchSetQuery(PatchSet(revision=msg['revision']))
            if ps == None:
                log.error('No corresponding patchset found for revision %s'
                        % (msg['revision']))
                return
            ps = ps[0]
        elif msg['action'] == 'PATCHSET.APPLY':
            ps = db.PatchSetQuery(PatchSet(id=msg['patchsetid']))
            if ps == None:
                log.error('No corresponding patchset found for revision %s'
                        % msg['revision'])
                return
            ps = ps[0]

        if ps:
            # remove it from the queue, error should have been comented to bug
            # XXX: (shall we confirm that here with bz_utils.has_coment?)
            bz.remove_whiteboard_tag('\[autoland-in-queue\]', ps.bug_id)
            db.PatchSetDelete(ps)
            log.debug('Received error on %s, deleting patchset %s'
                    % (msg['action'], ps.id))

def handle_patchset(patchset):
    """
    Message sent to HgPusher is of the JSON structure:
        {
          'job_type' : 'patchset',
          'bug_id' : 12345,
          'branch' : 'mozilla-central',
          'push_url' : 'ssh://hg.mozilla.org/try',
          'branch_url' : 'ssh://hg.mozilla.org/mozilla-central',
          'try_run' : 1,
          'try_syntax': '-p linux -u mochitests',
          'patchsetid' : 42L,
          'patches' :
                [
                    { 'id' : 54321,
                      'author' : { 'name' : 'Name',
                                   'email' : 'me@email.com' },
                      'reviews' : [
                            { 'reviewer' : { 'name' : 'Rev. Name',
                                             'email' : 'rev@email.com' },
                              'type' : 'superreview',
                              'result' : '+'
                            },
                            { ... }
                        ],
                      'approvals' : [
                            { 'approver' : { 'name' : 'App. Name',
                                             'email' : 'app@email.com' },
                              'type' : 'mozilla-esr10',
                              'result' : '+'
                            }
                        ]
                    },
                    { ... }
                ]
        }
    """
    log.debug('Handling patchset %s from queue.' % (patchset))

    # TODO: Check the retries & creation time.

    # Check permissions & patch set again, in case it has changed
    # since the job was put on the queue.
    patches = get_patchset(patchset.bug_id, user_patches=patchset.patchList())
    if patches == None:
        # Comment already posted in get_patchset. Full patchset couldn't be
        # processed.
        log.info("Patchset not valid. Deleting from database.")
        db.PatchSetDelete(patchset)
        return

    # get branch information
    branch = db.BranchQuery(Branch(name=patchset.branch))
    if not branch:
        # error, branch non-existent
        # XXX -- Should we email or otherwise let user know?
        log.error('Could not find %s in branches table.' % (patchset.branch))
        db.PatchSetDelete(patchset)
        return
    branch = branch[0]

    branch_perms = ldap.get_branch_permissions(branch.name)

    # double check if this job should be run
    if patchset.branch.lower() != 'try':
        r_status = get_review_status(patches, branch_perms)
        if r_status[0] == 'FAIL':
            log.info('Failed review on patches %s' % (','.join(r_status[1])))
            post_comment('Autoland Failure:\n%sFailed review on patch(es): %s'
                            % (' '.join(r_status[1])))
            return
        elif r_status[0] == 'PENDING':
            log.info('Missing required review for patches %s'
                        % (','.join(r_status[1])))
            post_comment('Autoland Failure:\n'
                         'Missing required review for patch(es): %s'
                            % (' '.join(r_status[1])))
            return
        elif r_status[0] == 'INVALID':
            log.info('Invalid review permissions on patches %s'
                    % (','.join(r_status[1])))
            post_comment('Autoland Failure:\n'
                         'Invalid review for patch(es): %s'
                            % (' '.join(r_status[1])))
            return
    if branch.approval_required:
        a_status = get_approval_status(patches, patchset.branch, branch_perms)
        if a_status[0] == 'FAIL':
            log.info('Failed approval on patches %s for branch %s'
                    % (','.join(r_status[1]), patchset.branch))
            post_comment('Autoland Failure:\n'
                        'Failed approval for branch %s on patch(es): %s'
                            % (patchset.branch, ' '.join(r_status[1])))
            return
        elif a_status[0] == 'PENDING':
            log.info('Require approval on patches %s for branch %s'
                    % (','.join(r_status[1]), patchset.branch))
            post_comment('Autoland Failure:\n'
                         'Missing required approval for branch %s '
                         'on patch(es): %s'
                         % (patchset.branch, ' '.join(r_status[1])))
            return
        elif r_status[0] == 'INVALID':
            log.info('Invalid approval permissions on patches %s'
                    % (','.join(r_status[1])))
            post_comment('Autoland Failure:\n'
                         'Invalid approval for patch(es): %s'
                            % (' '.join(r_status[1])))
            return

    if patchset.try_run:
        running = db.BranchRunningJobsQuery(Branch(name='try'))
        log.debug("Running jobs on try: %s" % (running))

        # get try branch info
        try_branch = db.BranchQuery(Branch(name='try'))
        if try_branch: try_branch = try_branch[0]
        else: return

        log.debug("Threshold for try: %s" % (try_branch.threshold))

        # ensure try is not above threshold
        if running >= try_branch.threshold:
            log.info("Too many jobs running on try right now.")
            return
        push_url = try_branch.repo_url
    else:   # branch landing
        running = db.BranchRunningJobsQuery(Branch(name=patchset.branch),
                                            count_try=False)
        log.debug("Running jobs on %s: %s" % (patchset.branch, running))

        log.debug("Threshold for branch: %s" % (branch.threshold))

        # ensure branch not above threshold
        if running >= branch.threshold:
            log.info("Too many jobs landing on %s right now." % (branch.name))
            return
        push_url = branch.repo_url

    message = { 'job_type':'patchset', 'bug_id':patchset.bug_id,
            'branch_url':branch.repo_url,
            'push_url':push_url,
            'branch':patchset.branch, 'try_run':patchset.try_run,
            'try_syntax':patchset.try_syntax,
            'patchsetid':patchset.id, 'patches':patches }

    log.info("Sending job to hgpusher: %s" % (message))
    mq.send_message(message, routing_key='hgpusher')
    patchset.push_time = datetime.datetime.utcnow()
    db.PatchSetUpdate(patchset)

def handle_comments():
    """
    Queries the Autoland DB for any outstanding comments to be posted.
    Gets the five oldest comments and tries to post them on the corresponding
    bug. In case of failure, the comments attempt count is updated, to be
    picked up again later.
    If we have attempted 5 times, get rid of the comment and log it.
    """
    comments = db.CommentGetNext(limit=5)   # Get up to 5 comments
    for comment in comments:
        # Note that notify_bug makes multiple retries
        success = bz.notify_bug(comment.comment, comment.bug)
        if success:
            # Posted. Get rid of it.
            db.CommentDelete(comment)
        elif comment.attempts == 5:
            # 5 attempts have been made, drop this comment as it is
            # probably not going anywhere.
            try:
                with open('failed_comments.log', 'a') as fc_log:
                    fc_log.write('%s\n\t%s'
                            % (comment.bug, comment.comment))
            except IOError:
                log.error('Unable to append to failed comments file.')
            log.error("Could not post comment to bug %s. Dropping comment: %s"
                    % (comment.bug, comment.comment))
            db.CommentDelete(comment.id)
        else:
            comment.attempts += 1
            db.CommentUpdate(comment)

def post_comment(comment, bug_id):
    """
    Post a comment that isn't in the comments db.
    Add it if posting fails.
    """
    success = bz.notify_bug(comment, bug_id)
    if success:
        log.info('Posted comment: "%s" to %s' % (comment, bug_id))
    else:
        log.info('Could not post comment to bug %s. Adding to comments table'
                % (bug_id))
        cmnt = Comment(comment=comment, bug=bug_id)
        db.CommentInsert(cmnt)

def main():
    mq.set_host(config['mq_host'])
    mq.set_exchange(config['mq_exchange'])
    mq.connect()
    mq.declare_and_bind(config['mq_autoland_queue'], 'db')

    log.setLevel(logging.DEBUG)
    LOGHANDLER.setFormatter(LOGFORMAT)
    log.addHandler(LOGHANDLER)

    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg == '--purge-queue':
                # purge the autoland queue
                mq.purge_queue(config['mq_autoland_queue'], prompt=True)
                exit(0)

    while True:
        # search bugzilla for any relevant bugs
        bz_search_handler()
        next_poll = time.time() + int(config['bz_poll_frequency'])

        if config.get('staging', False):
            # if this is a staging instance, launch schedulerDbPoller in order
            # to poll by revision. This will allow for posting back to
            # landfill.
            for revision in db.PatchSetGetRevs():
                cmd = ['bash', os.path.join(base_dir,
                                    'run_schedulerDbPoller_staging')]
                cmd.append(revision)
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
                (out, err) = proc.communicate()
                log.info('schedulerDbPoller Returned: %d' % (proc.returncode))
                log.info('stdout: %s' % (out))
                log.info('stderr: %s' % (err))

        while time.time() < next_poll:
            patchset = db.PatchSetGetNext()
            if patchset != None:
                handle_patchset(patchset)

            # take care of any comments that couldn't previously be posted
            handle_comments()

            # loop while we've got incoming messages
            while mq.get_message(config['mq_autoland_queue'],
                    message_handler):
                continue
            time.sleep(5)

if __name__ == '__main__':
    main()

