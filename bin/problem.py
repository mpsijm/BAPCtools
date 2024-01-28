import re
import argparse
import hashlib
import shlex
import sys

from pathlib import Path

import config
import latex
import parallel
import program
import run
from validate import Mode, Class
import validate
from util import *
from colorama import Fore, Style


# A problem.
class Problem:
    _SHORTNAME_REGEX_STRING = '^[a-z0-9]+$'
    _SHORTNAME_REGEX = re.compile(_SHORTNAME_REGEX_STRING)

    def __init__(self, path, tmpdir, label=None):
        # The problem name/shortname, which is the name of the directory and used as a display name.
        self.name = path.resolve().name
        # The Path of the problem directory.
        self.path = path
        self.tmpdir = tmpdir / self.name
        # Read problem.yaml and domjudge-problem.ini into self.settings Namespace object.
        self._read_settings()

        # Some caches.
        self._testcases = dict()
        self._submissions = None
        self._validators = dict()
        self._programs = dict()
        self._program_callbacks = dict()
        # Dictionary from path to parsed file contents.
        self._testdata_yamls = dict()

        # The label for the problem: A, B, A1, A2, X, ...
        self.label = label

        # TODO: transform this into nice warnings
        assert path.is_dir()
        if not Problem._SHORTNAME_REGEX.match(self.name):
            warn(
                f'Problem has a bad shortname: {self.name} does not match {self._SHORTNAME_REGEX_STRING}'
            )

        self.statement_languages = self._determine_statement_languages()

    def _determine_statement_languages(self):
        """Determine the languages that are both mentioned in the problem.yaml under name
        and have a corresponding problem statement.

        If problem.yaml's name key is a string, convert into dict; assume `en` as default language.
        """
        if isinstance(self.settings.name, str):
            self.settings.name = {'en': self.settings.name}
        yamllangs = set(self.settings.name)
        texlangs = set(
            path.suffixes[0][1:] for path in glob(self.path, 'problem_statement/problem.*.tex')
        )
        for lang in texlangs - yamllangs:
            error(
                f"{self.name}: Found problem.{lang}.tex, but no corresponding name in problem.yaml."
            )
        for lang in yamllangs - texlangs:
            error(
                f"{self.name}: Found name for language {lang} in problem.yaml, but not problem.{lang}.tex."
            )
        # Check that names in problem.yaml and \problemname{} in problem.*.tex agree:
        for lang in texlangs & yamllangs:
            unnormalised_yamlname = self.settings.name[lang]
            yamlname = ' '.join(unnormalised_yamlname.split())
            with open(self.path / 'problem_statement' / f'problem.{lang}.tex') as texfile:
                match texname := latex.get_argument_for_command(texfile, 'problemname'):
                    case None:
                        error(rf"No \problemname found in problem.{lang}.tex")
                        continue
                    case r'\\problemyamlname':
                        continue
                    case s if '\\' in s or '_' in s or '^' in s:
                        # texname contains markup, like "CO_2" or "\emph{Hello}":
                        # Assume authors know what they're doing
                        continue
                    case s if s != yamlname:
                        warn(
                            f'Problem titles in problem.{lang}.tex ({texname})'
                            + f' and problem.yaml ({yamlname}) differ;'
                            + r' consider using \problemname{\problemyamlname}.'
                        )
        return sorted(texlangs & yamllangs)

    def _read_settings(self):
        # some defaults
        self.settings = {
            'timelimit': 1.0,
            'timelimit_is_default': True,
            'timeout': 3,
            'name': '',
            'validation': 'default',
            'validator_flags': [],
            'author': '',
        }

        # parse problem.yaml
        if has_ryaml:
            try:
                yamldata = read_yaml_settings(self.path / 'problem.yaml')
            except ruamel.yaml.scanner.ScannerError:
                fatal('Make sure problem.yaml does not contain any more {% ... %}.')
        else:
            yamldata = read_yaml_settings(self.path / 'problem.yaml')

        if yamldata:
            for k, v in yamldata.items():
                self.settings[k] = v
            if 'timelimit' in yamldata:
                self.settings['timelimit_is_default'] = False

        # DEPRECATED: parse domjudge-problem.ini for the timelimit.
        domjudge_path = self.path / 'domjudge-problem.ini'
        if domjudge_path.is_file():
            verbose('domjudge-problem.ini is DEPRECATED. Use a .timelimit file instead.')
            for line in domjudge_path.read_text().splitlines():
                key, var = map(str.strip, line.strip().split('='))
                if (var[0] == '"' or var[0] == "'") and (var[-1] == '"' or var[-1] == "'"):
                    var = var[1:-1]
                if key == 'timelimit':
                    self.settings[key] = float(var)
                    self.settings['timelimit_is_default'] = False
                else:
                    self.settings[key] = var

        # Read the .timitlimit file if present.
        timelimit_path = self.path / '.timelimit'
        if timelimit_path.is_file():
            self.settings['timelimit'] = float(timelimit_path.read_text())
            self.settings['timelimit_is_default'] = False

        # Convert the dictionary to a namespace object.
        self.settings = argparse.Namespace(**self.settings)

        # Override settings by command line arguments.
        self.settings.timelimit = config.args.timelimit or self.settings.timelimit
        self.settings.timeout = int(config.args.timeout or 1.5 * self.settings.timelimit + 1)

        if self.settings.validation not in config.VALIDATION_MODES:
            fatal(
                f'Unrecognised validation mode {self.settings.validation}. Must be one of {", ".join(config.VALIDATION_MODES)}'
            )

        if isinstance(self.settings.validator_flags, str):
            self.settings.validator_flags = shlex.split(self.settings.validator_flags)

        self.interactive = self.settings.validation == 'custom interactive'

    # Walk up from absolute `path` (a file or directory) looking for the first testdata.yaml
    # file, and return its contents, or None if no testdata.yaml is found.
    def get_testdata_yaml(p, path):
        for dir in [path] + list(path.parents):
            f = dir / 'testdata.yaml'

            if f.is_file():
                # Store testdata.yaml files in a cache.
                if f not in p._testdata_yamls:
                    p._testdata_yamls[f] = read_yaml(f)
                return p._testdata_yamls[f]

            # Do not go above the data directory.
            if dir == p.path / 'data':
                break
        return None

    # statement_samples end in .in.statement and .ans.statement and are only used in the statement.
    def testcases(
        p,
        *,
        needans=True,
        needinteraction=False,
        only_sample=False,
        statement_samples=False,
        include_bad=False,
        copy=False,
    ):
        def maybe_copy(x):
            return x.copy() if copy and isinstance(x, (list, dict)) else x

        samplesonly = config.args.samples or only_sample

        if p.interactive:
            needans = False

        key = (needans, samplesonly, include_bad)
        if key in p._testcases is not None:
            return maybe_copy(p._testcases[key])

        in_paths = None
        if config.args.testcases:
            if samplesonly:
                assert False
            # Deduplicate testcases with both .in and .ans.
            in_paths = []
            for t in config.args.testcases:
                t = resolve_path_argument(p, t, 'data', suffixes=['.in'])
                if t:
                    # When running from contest level, the testcase must be inside the problem.
                    if config.level != 'problemset' or is_relative_to(problem.path, t):
                        if t.is_dir():
                            in_paths += glob(t, '**/*.in')
                        else:
                            in_paths.append(t)

            in_paths = list(set(in_paths))
        else:
            in_paths = list(glob(p.path, 'data/sample/**/*.in'))
            if statement_samples:
                in_paths += list(glob(p.path, 'data/sample/**/*.in.statement'))
            if not samplesonly:
                in_paths += list(glob(p.path, 'data/secret/**/*.in'))
            if include_bad:
                bad_paths = list(glob(p.path, 'data/bad/**/*.in'))
                if len(bad_paths) > 0:
                    warn(
                        'data/bad is deprecated. Use data/{invalid_inputs,invalid_outputs} instead.'
                    )
                in_paths += bad_paths
                in_paths += list(glob(p.path, 'data/invalid_inputs/**/*.in'))
                in_paths += list(glob(p.path, 'data/invalid_outputs/**/*.in'))

        testcases = []
        for f in in_paths:
            t = run.Testcase(p, f)
            # Require both in and ans files
            if needinteraction and not t.in_path.with_suffix('.interaction').is_file():
                assert only_sample
                warn(f'Found input file {f} without a .interaction file. Skipping.')
                continue
            if needans and not t.ans_path.is_file():
                if not t.bad_input:
                    warn(f'Found input file {f} without a .ans file. Skipping.')
                continue
            testcases.append(t)
        testcases.sort(key=lambda t: t.name)

        if len(testcases) == 0:
            if needinteraction:
                warn(f'Didn\'t find any testcases with interaction for {p.name}')
            else:
                warn(f'Didn\'t find any testcases{" with answer" if needans else ""} for {p.name}')
            testcases = False

        p._testcases[key] = testcases
        return maybe_copy(testcases)

    # returns a map {expected verdict -> [(name, command)]}
    def submissions(problem, accepted_only=False, copy=False):
        def maybe_copy(x):
            return x.copy() if copy and isinstance(x, (list, dict)) else x

        if problem._submissions is not None:
            return maybe_copy(problem._submissions.copy())

        paths = []
        if config.args.submissions:
            if accepted_only:
                accepted_only = 'all'

            def add(s):
                if s in paths:
                    warn(f'Ignoring duplicate submission: {s}')
                    return
                paths.append(s)

            for submission in config.args.submissions:
                s = resolve_path_argument(problem, submission, 'submissions')
                if s:
                    if s == problem.path / 'submissions':
                        paths += glob(s, '*/*')
                    elif s.parent == problem.path / 'submissions':
                        for s in glob(s, '*'):
                            add(s)
                    else:
                        # If running from a contest, the submission must be inside a problem.
                        if config.level == 'problem' or is_relative_to(problem.path, s):
                            add(s)
        else:
            for s in glob(problem.path / 'submissions', ('accepted/*' if accepted_only else '*/*')):
                if (
                    s.parent.name == 'time_limit_exceeded'
                    and config.RUNNING_TEST
                    and not config.TEST_TLE_SUBMISSIONS
                ):
                    continue

                paths.append(s)

        if len(paths) == 0:
            error('No submissions found!')
            problem._submissions = False
            return False

        programs = [run.Submission(problem, path) for path in paths]

        bar = ProgressBar('Build submissions', items=programs)

        def build_program(p):
            localbar = bar.start(p)
            p.build(localbar)
            localbar.done()

        p = parallel.Parallel(build_program)
        for pr in programs:
            p.put(pr)
        p.done()

        bar.finalize(print_done=False)

        submissions = dict()
        for verdict in config.VERDICTS:
            submissions[verdict] = []

        # Filter out broken submissions.
        for p in programs:
            if p.ok:
                submissions[p.expected_verdicts[0]].append(p)

        if sum(len(submissions[x]) for x in submissions) == 0:
            submissions = False
        problem._submissions = submissions
        if accepted_only == 'all':
            subs = []
            for x in submissions:
                subs += submissions[x]
            return subs
        if accepted_only:
            return maybe_copy(submissions['ACCEPTED'])
        return maybe_copy(submissions)

    # If check_constraints is True, this chooses the first validator that matches
    # contains 'constraints_file' in its source.
    # _validators maps from input/output to the list of validators.
    def validators(problem, validator_class: validate.Class, check_constraints=False) -> bool | list[validate.Validator]:
        """
        Args:
            validator_type: 'answer', 'output', 'input'
            check_constraints: True if the validator should check constraints

        Returns:
            False: something went wrong
            singleton list(OutputValidator) if validator_type is 'output'
            list(Validator) otherwise
        """
        assert isinstance(validator_class, validate.Class), validator_class

        key = (validator_class, check_constraints)
        if key in problem._validators:
            return problem._validators[key]
        ok = True

        paths_for_class = {
            vclass: [g for sdir in sdirs for g in glob(problem.path / sdir, '*')]
            for vclass, sdirs in validate.Class.subdirs().items()
        }

        # Handle default output validation
        if problem.settings.validation == 'default':
            if paths_for_class[Class.OUTPUT]:
                error("Validation is default but custom output validator exists (ignoring it)")
            paths_for_class[Class.OUTPUT] = [
                config.tools_root / 'support' / 'default_output_validator.cpp'
            ]

        paths = paths_for_class[validator_class]

        # Check that the proper number of validators is present
        match validator_class, len(paths):
            case Class.ANSWER, 0:
                log(f"No answer validator found")
            case Class.INPUT, 0:
                warn(f'No input validators found.')
            case Class.OUTPUT, l if l != 1:
                error(f'Found {len(paths)} output validators, expected exactly one.')
                ok = False

        # TODO: Instead of checking file contents, maybe specify this in generators.yaml?
        def has_constraints_checking(f):
            if not f.is_file():
                return False
            try:
                return 'constraints_file' in f.read_text()
            except UnicodeDecodeError:
                return False

        if check_constraints:
            paths = [
                f
                for f in paths
                if any(
                    has_constraints_checking(source)
                    for source in ([f] if f.is_file() else glob(f, '**/*'))
                )
            ]

        validator_dispatcher = {
            Class.INPUT: validate.InputValidator,
            Class.ANSWER: validate.AnswerValidator,
            Class.OUTPUT: validate.OutputValidator,
        }
        skip_double_build_warning = check_constraints or not paths_for_class[Class.ANSWER]
        validators = [
            validator_dispatcher[validator_class](
                problem,
                path,
                skip_double_build_warning=skip_double_build_warning,
                check_constraints=check_constraints,
            )
            for path in paths
        ]

        bar = ProgressBar(f'Build {validator_class} validators', items=validators)
        build_ok = True

        def build_program(p):
            nonlocal build_ok
            localbar = bar.start(p)
            build_ok &= p.build(localbar)
            localbar.done()

        p = parallel.Parallel(build_program)
        for pr in validators:
            p.put(pr)
        p.done()

        bar.finalize(print_done=False)

        # All validators must build.
        result = validators if ok and build_ok else False

        problem._validators[key] = result
        return validators

    def run_submissions(problem):
        needans = False if problem.interactive else True
        testcases = problem.testcases(needans=needans)

        if testcases is False:
            return False

        if problem.interactive:
            validators = problem.validators(Class.OUTPUT)
            if not validators:
                return False

        submissions = problem.submissions()
        if not submissions:
            return False

        max_submission_len = max([len(x.name) for cat in submissions for x in submissions[cat]])

        # Pre build all output validators to prevent nested ProgressBars.
        if problem.validators(Class.OUTPUT) is False:
            return False

        ok = True
        verdict_table = []
        # When true, the ProgressBar will print a newline before the first error log.
        needs_leading_newline = False if config.args.verbose else True
        for verdict in submissions:
            for submission in submissions[verdict]:
                d = dict()
                verdict_table.append(d)
                submission_ok, printed_newline = submission.run_all_testcases(
                    max_submission_len, table_dict=d, needs_leading_newline=needs_leading_newline
                )
                needs_leading_newline = not printed_newline
                ok &= submission_ok

        if config.args.table:
            Problem._print_table(verdict_table, testcases, submissions)

        return ok

    # Takes a list of submissions and runs them against the chosen testcases.
    # Instead of validating the output, this function just prints all output to the
    # terminal.
    # Note: The CLI only accepts one submission.
    def test_submissions(problem):
        submissions = problem.submissions()
        if submissions is False:
            return False

        for verdict in submissions:
            for submission in submissions[verdict]:
                if config.args.interactive:
                    submission.test_interactive()
                else:
                    submission.test()
        return True

    @staticmethod
    def _print_table(verdict_table, testcases, submission):
        # Begin by aggregating bitstrings for all testcases, and find bitstrings occurring often (>=config.TABLE_THRESHOLD).
        def single_verdict(row, testcase):
            if testcase.name in row:
                if row[testcase.name]:
                    return Fore.GREEN + '1' + Style.RESET_ALL
                else:
                    return Fore.RED + '0' + Style.RESET_ALL
            else:
                return '-'

        make_verdict = lambda tc: ''.join(map(lambda row: single_verdict(row, tc), verdict_table))
        resultant_count, resultant_id = dict(), dict()
        special_id = 0
        for testcase in testcases:
            resultant = make_verdict(testcase)
            if resultant not in resultant_count:
                resultant_count[resultant] = 0
            resultant_count[resultant] += 1
            if resultant_count[resultant] == config.TABLE_THRESHOLD:
                special_id += 1
                resultant_id[resultant] = special_id

        scores = {}
        for t in testcases:
            scores[t.name] = 0
        for dct in verdict_table:
            failures = 0
            for t in dct:
                if not dct[t]:
                    failures += 1
            for t in dct:
                if not dct[t]:
                    scores[t] += 1.0 / failures
        scores_list = sorted(scores.values())

        print(
            '\nVerdict analysis table. Submissions are ordered per column as above. Higher '
            'scores indicate they are critical to break some submissions. Only cases breaking at least one submission are listed.',
            file=sys.stderr,
        )
        print(f'{Fore.RED}0{Style.RESET_ALL}: submission fails testcase', file=sys.stderr)
        print(f'{Fore.GREEN}1{Style.RESET_ALL}: submission passes testcase\n', file=sys.stderr)

        for testcase in testcases:
            # Skip all AC testcases
            if all(map(lambda row: testcase.name in row and row[testcase.name], verdict_table)):
                continue

            color = Style.RESET_ALL
            if len(scores_list) > 6 and scores[testcase.name] >= scores_list[-6]:
                color = Fore.YELLOW
            if len(scores_list) > 3 and scores[testcase.name] >= scores_list[-3]:
                color = Fore.RED
            print(f'{str(testcase.name):<60}', end=' ', file=sys.stderr)
            resultant = make_verdict(testcase)
            print(resultant, end='  ', file=sys.stderr)
            print(
                f'{color}{scores[testcase.name]:0.3f}{Style.RESET_ALL}  ', end='', file=sys.stderr
            )
            if resultant in resultant_id:
                print(str.format('(Type {})', resultant_id[resultant]), end='', file=sys.stderr)
            print(end='\n', file=sys.stderr)

    def reset_testcase_hashes(self):
        self._testcase_hashes = {}

    # Returns None for new testcases or the Testcase object it equals.
    def matches_existing_testcase(self, t):
        if t.bad_input or t.bad_output:
            return None
        d = t.in_path.read_text()
        if d in self._testcase_hashes:
            return self._testcase_hashes[d]
        self._testcase_hashes[d] = t
        return None




    def validate_data(problem, mode:Mode, constraints: dict|bool|None=None) -> bool:
        """ Validate aspects of the test data """
        if constraints is True:
            constraints = {}
        assert constraints is None or isinstance(constraints, dict)

        if problem.interactive and mode != Mode.INPUT:
            log('Only input-validating interactive problem.')
            return True

        ok = True
        match mode:
            case Mode.INPUT:
                ok &= problem.validate_format(Class.INPUT, constraints)
            case Mode.ANSWER:
                ok &= problem.validate_format(Class.ANSWER, constraints)
                ok &= problem.validate_format(Class.OUTPUT, constraints)
            case Mode.OUTPUT | _ :
                assert False, "not implemented"
        return ok


    def validate_format(problem, validator_class:Class, constraints=None) -> bool:
        validators = problem.validators(validator_class, check_constraints=constraints is not None)

        if not validators:
            return False

        needans = validator_class != Class.INPUT
        testcases = problem.testcases(needans=needans, include_bad=True)

        if testcases is False:
            return True

        if len(testcases) == 0:
            return True

        action = 'Validating ' + str(validator_class)

        success = True

        problem.reset_testcase_hashes()

        # validate the testcases
        bar = ProgressBar(action, items=[t.name for t in testcases])
        for testcase in testcases:
            bar.start(testcase.name)

            if validator_class == Class.INPUT and not testcase.included:
                t2 = problem.matches_existing_testcase(testcase)
                if t2 is not None:
                    bar.error(f'Duplicate testcase: identical to {t2.name}')
                    ok = False
                    continue

            # When validating answer format with output validator
            # always be sensitive
            args = [
                    'case_sensitive', 'space_change_sensitive'
                    ] if validator_class == Class.OUTPUT else []

            success &= testcase.validate_format(validator_class, bar=bar, constraints=constraints, args=args)
            bar.done()

        bar.finalize(print_done=True)

        # Make sure all constraints are satisfied.
        if constraints:
            for loc, value in sorted(constraints.items()):
                loc = Path(loc).name
                name, has_low, has_high, vmin, vmax, low, high = value
                if not has_low:
                    warn(
                        f'BOUND NOT REACHED: `{name}` never equals lower bound {low}. Min value found: {vmin}'
                    )
                if not has_high:
                    warn(
                        f'BOUND NOT REACHED: `{name}` never equals upper bound {high}. Max value found: {vmax}'
                    )
                success = False

        return success
