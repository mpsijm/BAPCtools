import re
import shutil
import stat
import subprocess
import threading
from typing import Final, TYPE_CHECKING

from colorama import Fore

from util import *

if TYPE_CHECKING:  # Prevent circular import: https://stackoverflow.com/a/39757388
    from problem import Problem

EXTRA_LANGUAGES: Final[str] = """
checktestdata:
    name: 'Checktestdata'
    priority: 1
    files: '*.ctd'
    run: 'checktestdata {mainfile}'

viva:
    name: 'Viva'
    priority: 2
    files: '*.viva'
    run: 'java -jar {viva_jar} {mainfile}'

manual:
    name: 'manual'
    priority: 9999
    files: 'build run'
    compile: '{build}'
    run: '{run}'
"""

SANITIZER_FLAGS: Final[str] = """
cpp:
    compile: -fsanitize=undefined,address
"""

# The cached languages.yaml for the current contest.
_languages = None
_sanitizer = None
_program_config_lock = threading.Lock()


def languages():
    global _languages, _program_config_lock
    with _program_config_lock:
        if _languages is not None:
            return _languages

        if Path("languages.yaml").is_file():
            _languages = read_yaml(Path("languages.yaml"))
        else:
            _languages = read_yaml(config.TOOLS_ROOT / "config/languages.yaml")

        # Add custom languages.
        extra_langs = parse_yaml(EXTRA_LANGUAGES)
        for lang in extra_langs:
            assert lang not in _languages
            _languages[lang] = extra_langs[lang]

        return _languages


def sanitizer():
    global _sanitizer, _program_config_lock
    with _program_config_lock:
        if _sanitizer is not None:
            return _sanitizer

        # Read sanitizer extra flags
        _sanitizer = parse_yaml(SANITIZER_FLAGS)

        return _sanitizer


# A Program is class that wraps a program (file/directory) on disk. A program is usually one of:
# - a submission
# - a validator
# - a generator
# - a visualizer
#
# Constructor parameters:
# - problem: The Problem to which this Program belongs
# - path: main source file/directory: an absolute or relative path ('problem/generators/gen.py')
# - subdir: the subdirectory in which this Program belongs ('generators', 'submissions', ...)
# - deps (optional): a list of dependencies (must be Path objects)
#
# Member variables are:
# - short_path:     the path relative to problem/subdir/, or None
# - tmpdir:         the build directory in tmpfs. This is only created when build() is called.
# - input_files:    list of source files linked into tmpdir
# - language:       the detected language
# - env:            the environment variables used for compile/run command substitution
# - hash:           a hash of all of the program including all source files
# - limits          a dict of the optional limts, keys are:
#                   - code
#                   - compilation_time
#                   - compilation_memory
#                   - timeout
#                   - memory
#
# After build() has been called, the following are available:
# - run_command:    command to be executed. E.g. ['/path/to/run'] or ['python3', '/path/to/main.py']. `None` if something failed.
#
# build() will return the (run_command, message) pair.
class Program:
    input_files: list[Path]  # Populated in Program.build

    def __init__(
        self,
        problem: "Problem",
        path: Path,
        subdir: str,
        deps: Optional[list[Path]] = None,
        *,
        skip_double_build_warning=False,
        limits: dict[str, int] = {},
    ):
        if deps is not None:
            assert isinstance(self, Generator)
            assert isinstance(deps, list)
            assert len(deps) > 0

        assert self.__class__ is not Program  # Program is abstract and may not be instantiated

        # Make sure we never try to build the same program twice. That'd be stupid.
        if not skip_double_build_warning:
            if path in problem._programs:
                error(f"Why would you build {path} twice?")
                assert path not in problem._programs
            problem._programs[path] = self

        self.problem = problem
        self.path = path
        self.subdir = subdir

        # Set self.name and self.tmpdir.
        # Ideally they are the same as the path inside the problem, but fallback to just the name.
        try:
            # Only resolve the parent of the program. This preserves programs that are symlinks to other directories.
            relpath = (path.parent.resolve() / path.name).relative_to(
                problem.path.resolve() / self.subdir
            )
            self.short_path = relpath
            self.name: str = str(relpath)
            self.tmpdir = problem.tmpdir / self.subdir / relpath
        except ValueError:
            self.short_path = Path(path.name)
            self.name = str(path.name)
            self.tmpdir = problem.tmpdir / self.subdir / path.name

        self.compile_command: Optional[list[str]] = None
        self.run_command: Optional[list[str]] = None
        self.hash: Optional[str] = None
        self.env: dict[str, int | str | Path] = {}
        self.limits: dict[str, int] = limits

        self.ok = True
        self.built = False

        # Detect language, dependencies, and main file
        if deps:
            self.source_files = deps
            self.has_deps = True
        else:
            if path.is_dir():
                self.source_files = list(glob(path, "*"))
                # Filter out __pycache__ files.
                self.source_files = list(
                    filter(lambda f: f.name != "__pycache__", self.source_files)
                )
            elif path.is_file():
                self.source_files = [path]
            else:
                self.source_files = []
            self.has_deps = False

    # is file at path executable
    @staticmethod
    def _is_executable(path):
        return path.is_file() and (
            path.stat().st_mode & (stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        )

    # Returns true when file f matches the given shebang regex.
    @staticmethod
    def _matches_shebang(f, shebang):
        if shebang is None:
            return True
        with f.open() as o:
            return shebang.search(o.readline())

    # Do not warn for the same fallback language multiple times.
    warn_cache: set[str] = set()

    language: Optional[str]

    # Sets self.language and self.env['mainfile']
    def _get_language(self, bar: ProgressBar):
        fallback = False
        candidates = []
        for lang in languages():
            lang_conf = languages()[lang]
            globs = lang_conf["files"].split() or []
            shebang = re.compile(lang_conf["shebang"]) if lang_conf.get("shebang") else None
            priority = int(lang_conf["priority"])

            matching_files = []
            for f in self.input_files:
                if any(f.match(glob) for glob in globs) and Program._matches_shebang(f, shebang):
                    matching_files.append(f)

            if len(matching_files) == 0:
                continue

            candidates.append(
                (priority // 1000, len(matching_files), priority, lang, matching_files)
            )
        candidates.sort(reverse=True)

        for _, _, priority, lang, files in candidates:
            lang_conf = languages()[lang]
            name = lang_conf["name"]

            # Make sure we can run programs for this language.
            if "compile" in lang_conf:
                exe = lang_conf["compile"].split()[0]
                if exe[0] != "{" and shutil.which(exe) is None:
                    fallback = True
                    if exe not in Program.warn_cache:
                        if config.args.verbose:
                            bar.debug(
                                f"Compile program {exe} not found for language {name}. Falling back to lower priority languages."
                            )
                            Program.warn_cache.add(exe)
                    continue
            assert "run" in lang_conf
            exe = lang_conf["run"].split()[0]
            if exe[0] != "{" and shutil.which(exe) is None:
                fallback = True
                if exe not in Program.warn_cache:
                    if config.args.verbose:
                        Program.warn_cache.add(exe)
                        bar.debug(
                            f"Run program {exe} not found for language {name}. Falling back to lower priority languages."
                        )
                continue

            if fallback:
                if lang not in Program.warn_cache:
                    if config.args.verbose:
                        Program.warn_cache.add(lang)
                        bar.debug(f"Falling back to {languages()[lang]['name']}.")

            if len(files) == 0:
                self.ok = False
                bar.error(f"No file detected for language {name} at {self.path}.")
                return False

            self.language = lang
            mainfile = None
            if not self.has_deps:
                if len(files) == 1:
                    mainfile = files[0]
                else:
                    for f in files:
                        if f.name.lower().startswith("main"):
                            mainfile = f
                    mainfile = mainfile or sorted(files)[0]
            else:
                mainfile = self.tmpdir / self.source_files[0].name

            mainclass = str(mainfile.with_suffix("").name)
            self.env = {
                "path": str(self.tmpdir),
                # NOTE: This only contains files matching the winning language.
                "files": " ".join(str(f) for f in files),
                "binary": self.tmpdir / "run",
                "mainfile": str(mainfile),
                "mainclass": mainclass,
                "Mainclass": mainclass[0].upper() + mainclass[1:],
                # Memory limit in MB.
                "memlim": self.limits.get("memory", 2048),
                # Out-of-spec variables used by 'manual' and 'Viva' languages.
                "build": (
                    self.tmpdir / "build" if (self.tmpdir / "build") in self.input_files else ""
                ),
                "run": self.tmpdir / "run",
                "viva_jar": config.TOOLS_ROOT / "third_party/viva/viva.jar",
            }

            return True

        # The for loop did not find a suitable language.
        self.ok = False
        bar.error(f"No language detected for {self.path}.")
        return False

    def _checks(self, bar: ProgressBar):
        # Make sure C++ does not depend on stdc++.h, because it's not portable.
        if self.language == "cpp":
            for f in self.source_files:
                try:
                    if f.read_text().find("bits/stdc++.h") != -1:
                        if "validators/" in str(f):
                            bar.error("Must not depend on bits/stdc++.h.", resume=True)
                            break
                        else:
                            bar.log("Should not depend on bits/stdc++.h")
                            break
                except UnicodeDecodeError:
                    pass

        # Warn for known bad (non-deterministic) patterns in generators
        from validate import Validator

        if isinstance(self, Generator) or isinstance(self, Validator):
            if self.language == "cpp":
                for f in self.source_files:
                    try:
                        text = f.read_text()
                        bad_random = set()
                        for s in [
                            "rand\\(\\)",
                            "uniform_int_distribution",
                            "uniform_real_distribution",
                            "normal_distribution",
                            "exponential_distribution",
                            "geometric_distribution",
                            "binomial_distribution",
                            "random_device",
                            "default_random_engine",
                        ]:
                            for line in text.splitlines():
                                if s in line and "bt ignore" not in line:
                                    bad_random.add(s)
                        if bad_random:
                            bad_message = ", ".join(bad_random)
                            bar.warn(
                                f"Calling {bad_message} in {f.name} is implementation dependent in C++. Use <validation.h> instead, or add `// bt ignore` to the line."
                            )
                        if text.find("typeid(") != -1:
                            bar.warn(
                                f"Calling typeid() in {f.name} is implementation dependent in C++."
                            )
                    except UnicodeDecodeError:
                        pass
            if self.language and "py" in self.language:
                for f in self.source_files:
                    try:
                        text = f.read_text()
                        for s in ["list(set("]:
                            if text.find(s) != -1:
                                bar.warn(
                                    "The order of sets is not fixed across implementations. Please sort the list!"
                                )
                    except UnicodeDecodeError:
                        pass

    # Return True on success.
    def _compile(self, bar: ProgressBar):
        meta_path = self.tmpdir / "meta_.yaml"

        # Remove all non-source files.
        for f in self.tmpdir.glob("*"):
            if f not in (self.input_files + [meta_path]):
                if f.is_dir() and not f.is_symlink():
                    shutil.rmtree(f)
                else:
                    f.unlink()

        # The case where compile_command='{build}' will result in an empty list here.
        if not self.compile_command:
            return True

        try:
            ret = exec_command(
                self.compile_command,
                stdout=subprocess.PIPE,
                cwd=self.tmpdir,
                # Compile errors are never cropped.
                crop=False,
                timeout=self.limits.get("compilation_time", None),
                memory=self.limits.get("compilation_memory", None),
            )
        except FileNotFoundError as err:
            self.ok = False
            bar.error("Failed", str(err))
            return False

        if not ret.status:
            data = ""
            if ret.err is not None:
                data += strip_newline(ret.err) + "\n"
            if ret.out is not None:
                data += strip_newline(ret.out) + "\n"
            self.ok = False
            bar.error("Failed", data)
            return False

        write_yaml(
            {"hash": self.hash, "command": self.compile_command},
            meta_path,
            allow_yamllib=True,
        )
        return True

    # Return True on success, False on failure.
    def build(self, bar: ProgressBar):
        assert not self.built
        self.built = True

        if not self.ok:
            return False

        if len(self.source_files) == 0:
            self.ok = False
            if self.path.is_dir():
                bar.error(f"{self.short_path} is an empty directory.")
            else:
                bar.error(f"{self.path} does not exist.")
            return False

        # Check file names.
        for f in self.source_files:
            if not config.COMPILED_FILE_NAME_REGEX.fullmatch(f.name):
                self.ok = False
                bar.error(f"{str(f)} does not match file name regex {config.FILE_NAME_REGEX}")
                return False

        # Link all source_files
        if self.tmpdir.is_file():
            self.tmpdir.unlink()
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.input_files = []
        hashes = []
        for f in self.source_files:
            ensure_symlink(self.tmpdir / f.name, f)
            self.input_files.append(self.tmpdir / f.name)
            if not f.is_file():
                self.ok = False
                bar.error(f"{str(f)} is not a file")
                return False
            hashes.append(hash_file(f))
        self.hash = combine_hashes(hashes)

        if not self._get_language(bar):
            return False

        self._checks(bar)

        # A file containing the compile command and hash.
        meta_path = self.tmpdir / "meta_.yaml"

        lang_config = languages()[self.language]
        sanitizer_config = sanitizer()

        compile_command = lang_config["compile"] if "compile" in lang_config else ""
        run_command = lang_config["run"]

        if (
            self.subdir == "submissions"
            and config.args.sanitizer
            and self.language in sanitizer_config
        ):
            if "compile" in sanitizer_config[self.language]:
                compile_command += " " + sanitizer_config[self.language]["compile"]
            if "run" in sanitizer_config[self.language]:
                run_command += " " + sanitizer_config[self.language]["run"]

        self.compile_command = compile_command.format(**self.env).split()
        self.run_command = run_command.format(**self.env).split()

        # Compare the hash to the last build.
        up_to_date = False
        if meta_path.is_file():
            meta_yaml = read_yaml(meta_path)
            up_to_date = (
                meta_yaml["hash"] == self.hash and meta_yaml["command"] == self.compile_command
            )

        if not up_to_date or config.args.force_build:
            if not self._compile(bar):
                return False

        if self.path in self.problem._program_callbacks:
            for c in self.problem._program_callbacks[self.path]:
                c(self)

        if "code" in self.limits:
            size = sum(f.stat().st_size for f in self.source_files)
            if size > self.limits["code"] * 1024:
                bar.warn(
                    f"Code limit exceeded (set limits.code to at least {(size + 1023) // 1024}KiB in problem.yaml)"
                )

        return True

    def _exec_command(self, *args, **kwargs):
        if "timeout" not in kwargs and "timeout" in self.limits:
            kwargs["timeout"] = self.limits["timeout"]
        if "memory" not in kwargs and "memory" in self.limits:
            kwargs["memory"] = self.limits["memory"]
        return exec_command(*args, **kwargs)

    @staticmethod
    def add_callback(problem, path, c):
        if path not in problem._program_callbacks:
            problem._program_callbacks[path] = []
        problem._program_callbacks[path].append(c)


class Generator(Program):
    def __init__(self, problem: "Problem", path: Path, **kwargs):
        super().__init__(
            problem,
            path,
            "generators",
            limits={"timeout": problem.limits.generator_time},
            **kwargs,
        )

    # Run the generator in the given working directory.
    # May write files in |cwd| and stdout is piped to {name}.in if it's not written already.
    # Returns ExecResult. Success when result.status == ExecStatus.ACCEPTED.
    def run(self, bar, cwd, name, args=[]):
        assert self.run_command is not None

        in_path = cwd / (name + ".in")
        stdout_path = cwd / (name + ".in_")

        # Clean the directory, but not the meta_.yaml file.
        for f in cwd.iterdir():
            if f.name == "meta_.yaml":
                continue
            if f.is_dir() and not f.is_symlink():
                shutil.rmtree(f)
            else:
                f.unlink()

        timeout = self.limits["timeout"]

        with stdout_path.open("w") as stdout_file:
            result = self._exec_command(
                self.run_command + args,
                stdout=stdout_file,
                cwd=cwd,
            )

        result.retry = False

        if result.status == ExecStatus.TIMEOUT:
            # Timeout -> stop retrying and fail.
            bar.log(f"TIMEOUT after {timeout}s", color=Fore.RED)
            return result

        if not result.status:
            # Other error -> try again.
            result.retry = True
            return result

        if stdout_path.read_text():
            if in_path.is_file():
                bar.warn(f"Generator wrote to both {name}.in and stdout. Ignoring stdout.")
            else:
                stdout_path.rename(in_path)
        else:
            if not in_path.is_file():
                bar.log(f"Did not write {name}.in and stdout is empty!", color=Fore.RED)
                result.status = ExecStatus.REJECTED
                return result

        return result


class Visualizer(Program):
    def __init__(self, problem: "Problem", path: Path, **kwargs):
        super().__init__(
            problem,
            path,
            "visualizers",
            limits={"timeout": problem.limits.visualizer_time},
            **kwargs,
        )

    # Run the visualizer.
    # Stdin and stdout are not used.
    def run(self, cwd, args=[]):
        assert self.run_command is not None
        return self._exec_command(
            self.run_command + args,
            cwd=cwd,
        )
