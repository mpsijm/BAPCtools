import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import tools

RUN_DIR = Path.cwd().resolve()


@pytest.fixture(scope='function')  # Fixture is setup and torn down for every test.
def setup_skel_problem(request):
    skel_dir = RUN_DIR / 'test/problems/skel'
    tmp_dir = TemporaryDirectory(prefix="bapctools_test_")
    problem_dir = Path(tmp_dir.name) / 'skel'
    shutil.copytree(skel_dir, problem_dir)
    os.chdir(problem_dir)
    try:
        tools.test(['tmp', '--clean'])
        yield
    finally:
        tools.test(['tmp', '--clean'])
        os.chdir(RUN_DIR)
        shutil.rmtree(problem_dir)
        tmp_dir.cleanup()


@pytest.mark.usefixtures('setup_skel_problem')
class TestUnit:
    def test_memory(self):
        shutil.copy('submissions/accepted/hello.py', 'submissions/run_time_error/hello.py')
        os.system(
            'sed -i "s/print/list(range(int(1e8)));print/" submissions/run_time_error/hello.py'
        )
        tools.test('run --no-generate -m 256'.split())

    @pytest.mark.parametrize(
        'params', [
            # (returncode, wall_time, expected)
            (0, 1.8, 'Wall time ( 1.800s) exceeds time limit (1.0s)'),
            (-9, 2.01, 'Wall time:  2.010s'),
        ]
    )
    def test_timeout(self, capsys, monkeypatch, params):
        import config
        import run

        returncode, wall_time, expected = params

        # We have to actually give the correct answer, so when mocking ExecResult, always return "16" for simplicity
        os.remove('data/secret/2.in')
        os.remove('data/secret/2.ans')

        def mock_exec(command, **kwargs):
            kwargs["stdout"].write(b'16')
            return run.ExecResult(
                returncode,
                run.ExecStatus.ACCEPTED if wall_time < 2 else run.ExecStatus.TIMEOUT,
                0.5,
                wall_time,
                wall_time >= 2,
                "",
                "16"
            )

        def mock_check(_):
            assert config.n_error == 1

        monkeypatch.setattr(run, "exec_command", mock_exec)
        monkeypatch.setattr(tools, "check_success", mock_check)

        tools.test('run --no-generate'.split())
        stdout, stderr = capsys.readouterr()
        assert expected in stderr
