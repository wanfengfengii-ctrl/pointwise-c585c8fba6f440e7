"""Single verification entry: every stored regression plus the public scenario.

Keeping this as one argv command lets the run manifest propagate any failure
with a non-zero exit code without nested command arrays.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(argv):
    return subprocess.run([sys.executable, '-B'] + argv, cwd=ROOT).returncode


def main():
    for argv in (['-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                 ['-m', 'tools.long_scenario']):
        code = run(argv)
        if code != 0:
            return code
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
