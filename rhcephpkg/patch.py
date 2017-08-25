import re
import subprocess
import tempfile
import six
from tambo import Transport
import rhcephpkg.util as util

BZ_REGEX = r'rhbz#(\d+)'


class BzNotFound(Exception):
    pass


def read_rules_file():
    """ Return contents of debian/rules as a single multiline string.  """
    with open('debian/rules') as fh:
        return fh.read()


def read_commit():
    """
    Return the current $COMMIT sha1 from debian/rules, or None if not found.
    """
    commit_re = r'export COMMIT=([0-9a-f]{40})'
    rules = read_rules_file()
    m = re.search(commit_re, rules)
    if m:
        return m.group(1)


def ensure_bzs(changelog):
    """
    Ensure every change has an associated RHBZ number.

    Raise BzNotFound if any changes lack RHBZ numbers.
    """
    missing = []
    for change in changelog:
        bzs = re.findall(BZ_REGEX, change)
        if not bzs:
            missing.append(change)
    if missing:
        raise BzNotFound(', '.join(missing))


class Patch(object):
    help_menu = 'apply patches from patch-queue branch'
    _help = """
Generate patches from a patch-queue branch.

Options:
--nobz    Do not require "Resolves: rhbz#" for every patch. The default is to
          require them. Use this CLI option to override the default.
"""
    name = 'patch'

    def __init__(self, argv):
        self.argv = argv
        self.options = ('--nobz',)

    def main(self):
        self.parser = Transport(self.argv, options=self.options)
        self.parser.catch_help = self.help()
        self.parser.parse_args()
        self._run()

    def help(self):
        return self._help

    def _run(self):
        """ Generate quilt patch series with gbp pq, and update d/rules """

        # Determine the names of the patch-queue branch and debian branch
        current_branch = util.current_branch()
        patches_branch = util.current_patches_branch()
        debian_branch = util.current_debian_branch()

        # TODO: default to fetching from upstream, the way rdopkg patch does.

        # Get the new sha1 to insert into the $COMMIT variable in d/rules
        cmd = ['git', 'rev-parse', patches_branch]
        output = subprocess.check_output(cmd)
        patches_sha1 = output.rstrip()
        if six.PY3:
            patches_sha1 = output.decode('utf-8').rstrip()

        # Switch to "debian" branch if necessary
        if current_branch != debian_branch:
            cmd = ['git', 'checkout', debian_branch]
            subprocess.check_call(cmd)

        # Get the original (old) patch series
        old_series = self.read_series_file('debian/patches/series')
        old_subjects = map(lambda x: x.subject, old_series)

        # Git-buildpackage pq operation
        cmd = ['gbp', 'pq', 'export']
        subprocess.check_call(cmd)

        # Bail early if gbp pq did nothing.
        cmd = ['git', 'status', '-s', 'debian/patches/']
        if subprocess.check_output(cmd) == '':
            print('No new patches, quitting.')
            raise SystemExit(1)

        # Add all patch files to Git's index
        cmd = ['git', 'add', '--all', 'debian/patches']
        subprocess.check_call(cmd)

        # Replace $COMMIT sha1 in d/rules
        old_sha1 = read_commit()
        if old_sha1:
            rules = read_rules_file()
            with open('debian/rules', 'w') as fileh:
                fileh.write(rules.replace(old_sha1, patches_sha1))

        # Get the new patch series
        new_series = self.read_series_file('debian/patches/series')
        # Select only the ones that are new (according to commit subjects)
        new_series = [p for p in new_series if p.subject not in old_subjects]

        if not new_series:
            # Maybe we rewrote some patch files in place?
            # Check Git itself for changed files:
            new_series = self.read_git_debian_patches()

        # Add patch entries to d/changelog
        changelog = self.generate_changelog(new_series)
        try:
            ensure_bzs(changelog)
        except BzNotFound:
            if not self.parser.has('--nobz'):
                raise
        util.bump_changelog(changelog)

        # Assemble a standard commit message string "clog".
        clog = "debian: %s\n" % util.get_deb_version()
        clog += "\n"
        clog += "Add patches from %s\n" % patches_branch
        clog += "\n"
        clog += util.format_changelog(changelog)

        # Commit everything with the standard commit message.
        with tempfile.NamedTemporaryFile() as temp:
            temp.write(clog)
            temp.flush()
            cmd = ['git', 'commit', 'debian/changelog', 'debian/patches',
                   'debian/rules', '-F', temp.name]
            subprocess.check_call(cmd)

        # Summarize this commit on STDOUT for the developer.
        # (This matches the behavior of "rdopkg patch".)
        cmd = ['git', '--no-pager', 'log', '--name-status', 'HEAD~..HEAD']
        subprocess.check_call(cmd)

    def generate_changelog(self, series):
        """
        Generate a list of changelog entries for this Patch series.

        :return: a list of strings
        """
        changelog = []
        for p in series:
            # If there was some in-place Git modification for this patch,
            # (.git_action attribute), include that in our log.
            try:
                action = p.git_action
                # Make common actions human-readable:
                if action == 'M':
                    action = 'Modified'
                if action == 'D':
                    action = 'Deleted'
                change = '%s %s' % (action, p.path)
            except AttributeError:
                # This was a simple patch addition, so just log the patch's
                # subject.
                change = p.subject
            bzs = self.get_rhbzs(p)
            bzstr = ' '.join(map(lambda x: 'rhbz#%s' % x, bzs))
            if bzstr != '':
                change += ' (%s)' % bzstr
            changelog.append(change)
        return changelog

    def get_rhbzs(self, patch):
        bzs = re.findall(BZ_REGEX, patch.subject)
        bzs.extend(re.findall(BZ_REGEX, patch.long_desc))
        return bzs

    def read_series_file(self, file_):
        try:
            from gbp.patch_series import PatchSeries
            return PatchSeries.read_series_file(file_)
        except ImportError:
            raise SystemExit(
                'Please run "sudo apt-get install git-buildpackage" to write '
                'the patches to ./debian/changelog')

    def read_git_debian_patches(self):
        """
        Load all edited Debian patches (from "git status") into Patch objects.

        The returned Patch objects have an extra ".git_action" attribute. Use
        this to determine what happened to the patch in Git.

        :return: a list of Patch objects
        """
        try:
            from gbp.patch_series import Patch
        except ImportError:
            raise SystemExit(
                'Please run "sudo apt-get install git-buildpackage"')
        cmd = ['git', 'status', '-s', 'debian/patches/']
        output = subprocess.check_output(cmd)
        if six.PY3:
            output = output.decode('utf-8')
        patches = []
        for line in output.splitlines():
            (action, filename) = line.split()
            patch = Patch(filename)
            # Hack: record what happened to this patch file:
            patch.git_action = action
            patches.append(patch)
        return patches
