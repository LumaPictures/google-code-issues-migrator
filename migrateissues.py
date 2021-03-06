#!/usr/bin/env python
# -*- coding: utf-8 -*-

import csv
import getpass
import logging
import optparse
import re
import sys
import urllib2
import time

from datetime import datetime

from github import Github
from github import GithubException
from pyquery import PyQuery as pq

logging.basicConfig(level = logging.ERROR)

# The maximum number of records to retrieve from Google Code in a single request

GOOGLE_MAX_RESULTS = 25

GOOGLE_ISSUE_TEMPLATE = '_Original issue: {}_'
GOOGLE_ISSUES_URL = 'https://code.google.com/p/{}/issues/csv?can=1&num={}&start={}&colspec=ID%20Type%20Status%20Owner%20Summary%20Opened%20Closed%20Reporter%20Stars&sort=id'
GOOGLE_URL = 'http://code.google.com/p/{}/issues/detail?id={}'
GOOGLE_URL_RE = 'http://code.google.com/p/%s/issues/detail\?id=(\d+)'
GOOGLE_ID_RE = GOOGLE_ISSUE_TEMPLATE.format(GOOGLE_URL_RE)

# The minimum number of remaining Github rate-limited API requests before we pre-emptively
# abort to avoid hitting the limit part-way through migrating an issue.

GITHUB_SPARE_REQUESTS = 50

# Mapping from Google Code issue labels to Github labels

LABEL_MAPPING = {
    'Type-Defect' : 'bug',
    'Type-Enhancement' : 'enhancement'
}

# Mapping from Google Code issue states to Github labels

STATE_MAPPING = {
    'invalid': 'invalid',
    'duplicate': 'duplicate',
    'wontfix': 'wontfix'
}

def stars_to_label(stars):
    '''Return a label string corresponding to a star range.

    For example, '1' -> '1 star', '2' -> '2-5 stars', etc.
    '''
    stars = int(stars)
    if stars == 1:
        return '1 star'
    elif stars <= 5:
        return '2–5 stars'
    elif stars <= 10:
        return '6–10 stars'
    elif stars <= 20:
        return '11–20 stars'
    else:
        return '21+ stars'

def output(string):
    sys.stdout.write(string)
    sys.stdout.flush()

def spacing_template(wordList, spacing=12):
    output = []
    template = '\t{0:%d}' % (spacing)
    for word in wordList:
        output.append(template.format(word))
    return ' : '.join(output)

def escape(s):
    """Process text to convert markup and escape things which need escaping"""
    if s:
        s = s.replace('%', '&#37;')  # Escape % signs
    return s

def transform_to_markdown_compliant(string):
    # Escape chars interpreted as markdown formatting by GH
    string = re.sub(r'(\s)~~', r'\1\\~~', string)
    string = re.sub(r'\n(\s*)>', r'\n\1\\>', string)
    string = re.sub(r'\n(\s*)#', r'\n\1\\#', string)
    string = re.sub(r'(?m)^-([- \r]*)$', r'\\-\1', string)
    # '==' is also making headers, but can't nicely escape ('\' shows up)
    string = re.sub(r'(\S\s*\n)(=[= ]*(\r?\n|$))', r'\1\n\2', string)
    # Escape < to avoid being treated as an html tag
    string = re.sub(r'(\s)<', r'\1\\<', string)
    # Avoid links that should not be links.
    # I can find no way to escape the # w/o using backtics:
    string = re.sub(r'(\s+)(#\d+)(\W)', r'\1`\2`\3', string)
    # Create issue links
    string = re.sub(r'\bi#(\d+)', r'issue #\1', string)
    string = re.sub(r'\bissue (\d+)', r'issue #\1', string)
    return string

def github_label(name, color = "FFFFFF"):
    """ Returns the Github label with the given name, creating it if necessary. """

    try:
        return label_cache[name]
    except KeyError:
        try:
            return label_cache.setdefault(name, github_repo.get_label(name))
        except GithubException:
            return label_cache.setdefault(name, github_repo.create_label(name, color))


def get_github_milestone(name):
    """ Returns the Github milestone with the given name, creating it if necessary. """

    try:
        return milestone_cache[name]
    except KeyError:
        for milestone in list(github_repo.get_milestones()):
            if milestone.title == name:
                return milestone_cache.setdefault(name, milestone)
        return milestone_cache.setdefault(name, github_repo.create_milestone(name))


def parse_gcode_date(date_text):
    """ Transforms a Google Code date into a more human readable string. """

    try:
        parsed = datetime.strptime(date_text, '%a %b %d %H:%M:%S %Y')
    except ValueError:
        return date_text

    return parsed.strftime("%B %d, %Y %H:%M:%S")


def add_issue_to_github(issue):
    """ Migrates the given Google Code issue to Github. """

    # Github rate-limits API requests to 5000 per hour, and if we hit that limit part-way
    # through adding an issue it could end up in an incomplete state.  To avoid this we'll
    # ensure that there are enough requests remaining before we start migrating an issue.

    if github.rate_limiting[0] < GITHUB_SPARE_REQUESTS:
        raise Exception('Aborting to to impending Github API rate-limit cutoff.')

    body = issue['content'].replace('%', '&#37;')

    output('Adding issue %d' % issue['gid'])

    if options.verbose:
        output('\n')
        outList = [
            spacing_template(['Title', issue['title']]),
            spacing_template(['State', issue['state']]),
            spacing_template(['Labels', issue['labels']]),
            spacing_template(['Milestone', issue['milestone']]),
            spacing_template(['Source link', issue['link']])
        ]
        output('\n'.join(outList))

    github_issue = None

    if not options.dry_run:
        github_labels = [github_label(label) for label in issue['labels']]
        text = body.encode('utf-8')
        text = transform_to_markdown_compliant(text)
        if issue['milestone']:
            github_milestone = get_github_milestone(issue['milestone'])
            github_issue = github_repo.create_issue(issue['title'], body = text, labels = github_labels,
                milestone = github_milestone)
        else:
            github_issue = github_repo.create_issue(issue['title'], body = text, labels = github_labels)
        if options.verbose and github_issue:
            output('\n')
            output(spacing_template(['Dest link', github_issue.url]))

    # Assigns issues that originally had an owner to the current user
    if issue['owner'] and options.assign_owner:
        assignee = github.get_user(github_user.login)
        if not options.dry_run:
            github_issue.edit(assignee = assignee)

    return github_issue


def add_comments_to_issue(github_issue, gcode_issue):
    """ Migrates all comments from a Google Code issue to its Github copy. """

    # Retrieve existing Github comments, to figure out which Google Code comments are new
    existing_comments = [comment.body for comment in github_issue.get_comments()]

    # Add any remaining comments to the Github issue
    output("\nSyncing comments ")
    for i, comment in enumerate(gcode_issue['comments']):
        body = u'_From {author} on {date}_\n\n{body}'.format(**comment)
        topost = transform_to_markdown_compliant(body)
        if topost in existing_comments:
            logging.info('Skipping comment %d: already present', i + 1)
        else:
            logging.info('Adding comment %d', i + 1)
            if options.verbose:
                output('\n\tAdd: From {author} on {date}'.format(**comment))
            if not options.dry_run:
                topost = topost.encode('utf-8')
                github_issue.create_comment(topost)

                # We use a delay to avoid comments being created on GitHub
                # in the wrong order, due to network non-determinism.
                # Without this delay, I consistently observed a full 1 in 3
                # GoogleCode issue comments being reordered.
                # XXX: querying GitHub in a loop to see when the comment has
                # been posted may be faster, but will cut into the rate limit.
                time.sleep(5)

def get_attachments(link, attachments):
    if not attachments:
        return ''

    body = u'\n\n'
    for attachment in (pq(a) for a in attachments):
        if not attachment('a'): # Skip deleted attachments
            continue

        # Linking to the comment with the attachment rather than the
        # attachment itself since Google Code uses download tokens for
        # attachments
        body += u'**Attachment:** [{}]({})'.format(attachment('b').text(), link)
    return body


def get_gcode_issue(issue_summary):
    def get_author(doc):
        userlink = doc('.userlink')
        return '[{}](https://code.google.com{})'.format(userlink.text(), userlink.attr('href'))

    # Populate properties available from the summary CSV
    issue = {
        'gid': int(issue_summary['ID']),
        'title': issue_summary['Summary'].replace('%', '&#37;'),
        'link': GOOGLE_URL.format(google_project_name, issue_summary['ID']),
        'owner': issue_summary['Owner'],
        'state': 'closed' if issue_summary['Closed'] else 'open',
        'date': datetime.fromtimestamp(float(issue_summary['OpenedTimestamp'])),
        'status': issue_summary['Status'].lower()
    }

    # Build a list of labels to apply to the new issue, including an 'imported' tag that
    # we can use to identify this issue as one that's passed through migration.
    # Also, build a milestone (google code sees it as a label)
    milestone = None
    labels = ['imported']
    for label in issue_summary['AllLabels'].split(', '):
        if label.startswith('Milestone-'):
            milestone = label.replace('Milestone-', '')
            continue
        if label.startswith('Priority-') and options.omit_priority:
            continue
        if not label:
            continue
        labels.append(LABEL_MAPPING.get(label, label))

    if options.migrate_stars and 'Stars' in issue_summary:
        labels.append(stars_to_label(issue_summary['Stars']))

    # Add additional labels based on the issue's state
    if issue['status'] in STATE_MAPPING:
        labels.append(STATE_MAPPING[issue['status']])

    issue['labels'] = labels
    issue['milestone'] = milestone

    # Scrape the issue details page for the issue body and comments
    opener = urllib2.build_opener()
    if options.google_code_cookie:
        opener.addheaders = [('Cookie', options.google_code_cookie)]
    # Missing issues may still exist in csv; make sure link is good
    try:
        connection = opener.open(issue['link'])
    except urllib2.HTTPError:
        return None
    encoding = connection.headers['content-type'].split('charset=')[-1]
    # Pass "ignore" so malformed page data doesn't abort us
    doc = pq(connection.read().decode(encoding, "ignore"))

    description = doc('.issuedescription .issuedescription')
    issue['author'] = get_author(description)

    issue['comments'] = []
    def split_comment(comment, text):
        # Github has an undocumented maximum comment size (unless I just failed
        # to find where it was documented), so split comments up into multiple
        # posts as needed.
        while text:
            comment['body'] = text[:7000]
            text = text[7000:]
            if text:
                comment['body'] += '...'
                text = '...' + text
            issue['comments'].append(comment.copy())

    split_comment(issue, description('pre').text())
    issue['content'] = u'_From {author} on {date:%B %d, %Y %H:%M:%S}_\n\n{content}{attachments}\n\n{footer}'.format(
            content = issue['comments'].pop(0)['body'],
            footer = GOOGLE_ISSUE_TEMPLATE.format(GOOGLE_URL.format(google_project_name, issue['gid'])),
            attachments = get_attachments(issue['link'], doc('.issuedescription .issuedescription .attachments')),
            **issue)

    issue['comments'] = []
    for comment in doc('.issuecomment'):
        comment = pq(comment)
        if not comment('.date'):
            continue # Sign in prompt line uses same class
        if comment.hasClass('delcom'):
            continue # Skip deleted comments

        date = parse_gcode_date(comment('.date').attr('title'))
        body = comment('pre').text()
        author = get_author(comment)

        updates = comment('.updates .box-inner')
        if updates:
            body += '\n\n' + updates.html().strip().replace('\n', '').replace('<b>', '**').replace('</b>', '**').replace('<br/>', '\n')

        body += get_attachments('{}#{}'.format(issue['link'], comment.attr('id')), comment('.attachments'))

        # Strip the placeholder text if there's any other updates
        body = body.replace('(No comment was entered for this change.)\n\n', '')

        split_comment({'date': date, 'author': author}, body)

    return issue

def get_gcode_issues():
    count = 100
    start_index = 0
    issues = []
    while True:
        url = GOOGLE_ISSUES_URL.format(google_project_name, count, start_index)
        issues.extend(row for row in csv.DictReader(urllib2.urlopen(url), dialect=csv.excel))

        if issues and 'truncated' in issues[-1]['ID']:
            issues.pop()
            start_index += count
        else:
            return issues


def process_gcode_issues(existing_issues):
    """ Migrates all Google Code issues in the given dictionary to Github. """

    issues = get_gcode_issues()
    previous_gid = 1

    if options.start_at is not None:
        issues = [x for x in issues if int(x['ID']) >= options.start_at]
        previous_gid = options.start_at - 1
        output('Starting at issue %d\n' % options.start_at)

    for issue in issues:
        issue = get_gcode_issue(issue)

        # problem occured getting issue information from url, may be deleted
        if issue is None:
            continue

        if options.skip_closed and (issue['state'] == 'closed'):
            continue

        # If we're trying to do a complete migration to a fresh Github project,
        # and want to keep the issue numbers synced with Google Code's, then we
        # need to create dummy closed issues for deleted or missing Google Code
        # issues.
        if options.synchronize_ids:
            for gid in xrange(previous_gid + 1, issue['gid']):
                if gid in existing_issues:
                    continue

                output('Creating dummy entry for missing issue %d\n' % gid)
                title = 'Google Code skipped issue %d' % gid
                body = '_Skipping this issue number to maintain synchronization with Google Code issue IDs._'
                footer = GOOGLE_ISSUE_TEMPLATE.format(GOOGLE_URL.format(google_project_name, gid))
                body += '\n\n' + footer
                github_issue = github_repo.create_issue(title, body = body, labels = [github_label('imported')])
                github_issue.edit(state = 'closed')
                existing_issues[previous_gid] = github_issue
            previous_gid = issue['gid']

        # Add the issue and its comments to Github, if we haven't already
        if issue['gid'] in existing_issues:
            github_issue = existing_issues[issue['gid']]
            output('Not adding issue %d (exists)' % issue['gid'])
        else:
            github_issue = add_issue_to_github(issue)

        if github_issue:
            add_comments_to_issue(github_issue, issue)
            if github_issue.state != issue['state']:
                github_issue.edit(state = issue['state'])
        output('\n')

        log_rate_info()

def get_existing_github_issues():
    """ Returns a dictionary of Github issues previously migrated from Google Code.

    The result maps Google Code issue numbers to Github issue objects.
    """

    output("Retrieving existing Github issues...\n")
    id_re = re.compile(GOOGLE_ID_RE % google_project_name)

    try:
        existing_issues = list(github_repo.get_issues(state='open')) + list(github_repo.get_issues(state='closed'))
        existing_count = len(existing_issues)
        issue_map = {}
        for issue in existing_issues:
            id_match = id_re.search(issue.body)
            if not id_match:
                continue

            google_id = int(id_match.group(1))
            issue_map[google_id] = issue
            labels = [l.name for l in issue.get_labels()]
            if not 'imported' in labels:
                # TODO we could fix up the label here instead of just warning
                logging.warn('Issue missing imported label %s- %r - %s', google_id, labels, issue.title)
        imported_count = len(issue_map)
        logging.info('Found %d Github issues, %d imported',existing_count,imported_count)
    except:
        logging.error('Failed to enumerate existing issues')
        raise
    return issue_map


def log_rate_info():
    logging.info('Rate limit (remaining/total) %r', github.rate_limiting)
    # Note: this requires extended version of PyGithub from tfmorris/PyGithub repo
    #logging.info('Rate limit (remaining/total) %s',repr(github.rate_limit(refresh=True)))

if __name__ == "__main__":
    usage = "usage: %prog [options] <google project name> <github username> <github project>"
    description = "Migrate all issues from a Google Code project to a Github project."
    parser = optparse.OptionParser(usage = usage, description = description)

    parser.add_option("-a", "--assign-owner", action = "store_true", dest = "assign_owner", help = "Assign owned issues to the Github user", default = False)
    parser.add_option("-d", "--dry-run", action = "store_true", dest = "dry_run", help = "Don't modify anything on Github", default = False)
    parser.add_option("-p", "--omit-priority", action = "store_true", dest = "omit_priority", help = "Don't migrate priority labels", default = False)
    parser.add_option("-s", "--synchronize-ids", action = "store_true", dest = "synchronize_ids", help = "Ensure that migrated issues keep the same ID", default = False)
    parser.add_option("-c", "--google-code-cookie", dest = "google_code_cookie", help = "Cookie to use for Google Code requests. Required to get unmangled names", default = '')
    parser.add_option('--skip-closed', action = 'store_true', dest = 'skip_closed', help = 'Skip all closed bugs', default = False)
    parser.add_option('--start-at', dest = 'start_at', help = 'Start at the given Google Code issue number', default = None, type = int)
    parser.add_option('--migrate-stars', action = 'store_true', dest = 'migrate_stars', help = 'Migrate binned star counts as labels', default = False)
    parser.add_option('--verbose', action = 'store_true', dest = 'verbose', help = 'Print more detailed information for each add.', default = False)

    options, args = parser.parse_args()

    if len(args) != 3:
        parser.print_help()
        sys.exit()

    label_cache = {} # Cache Github tags, to avoid unnecessary API requests
    milestone_cache = {}

    google_project_name, github_user_name, github_project = args

    while True:
        github_password = getpass.getpass("Github password: ")
        try:
            Github(github_user_name, github_password).get_user().login
            break
        except BadCredentialsException:
            print "Bad credentials, try again."

    github = Github(github_user_name, github_password)
    log_rate_info()
    github_user = github.get_user()

    # If the project name is specified as owner/project, assume that it's owned by either
    # a different user than the one we have credentials for, or an organization.

    if "/" in github_project:
        owner_name, github_project = github_project.split("/")
        try:
            github_owner = github.get_user(owner_name)
        except GithubException:
            try:
                github_owner = github.get_organization(owner_name)
            except GithubException:
                github_owner = github_user
    else:
        github_owner = github_user

    github_repo = github_owner.get_repo(github_project)

    try:
        existing_issues = get_existing_github_issues()
        log_rate_info()
        process_gcode_issues(existing_issues)
    except Exception:
        parser.print_help()
        raise
