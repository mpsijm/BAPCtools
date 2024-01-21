import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import tools

RUN_DIR = Path.cwd().resolve()


@pytest.fixture(scope='class')
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
        shutil.copy('submissions/accepted/hello.cpp', 'submissions/run_time_error/hello.cpp')
        os.system(
            'sed -i "s/cin >> n/auto huge = new int[100000000]; cin >> huge[42]; n = huge[42]/" submissions/run_time_error/hello.cpp'
        )
        tools.test('run -m 256'.split())
