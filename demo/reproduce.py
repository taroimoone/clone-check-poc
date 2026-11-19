#!/usr/bin/env python3
"""Reproduce clone drift and time automatic C# clone discovery."""

import argparse
from pathlib import Path
import platform
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "ci/clone_check.py"
A = "legacy/production/LineA.cs"
B = "products/new/assembly/controls/LineB.cs"
SIMILAR = "another/Assembly/Similar.cs"
NOISE = "modules/shared/Noise.cs"

OLD = """        var trimmed = value.Trim();
        return trimmed.Length >= {threshold} && trimmed.StartsWith("item");"""
NEW = """        if (value is null)
            return false;
""" + OLD


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


def code(name, methods):
    return f"public static class {name}\n{{\n" + "".join(methods) + "}\n"


def method(number, body=OLD, name="Accept", parameter="value"):
    function = f"{name}{number}"
    body = body.format(threshold=number + 5).replace("value", parameter)
    return f"    public static bool {function}(string? {parameter})\n    {{\n{body}\n    }}\n"


def write(repo, path, contents):
    destination = repo / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(contents, encoding="utf-8")


def save(repo, message):
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)


def fixture(repo, count, noise_files):
    git(repo, "init", "-qb", "main")
    git(repo, "config", "user.name", "Clone Check Demo")
    git(repo, "config", "user.email", "demo@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.hooksPath", str(repo / ".git" / "disabled-hooks"))
    git(repo, "config", "core.autocrlf", "false")

    for index in range(noise_files):
        write(repo, f"unrelated/Component{index:05}.cs",
              code(f"Component{index}", [method(index + 10000)]))
    write(repo, NOISE, code("Noise", [method(123456)]))
    write(repo, A, code("LineA", [method(index) for index in range(count)]))
    write(repo, SIMILAR, code("Similar", [method(399, name="Validate")]))
    save(repo, "main: add line A and unrelated code")
    git(repo, "tag", "clone-point")

    git(repo, "switch", "-qc", "branch1")
    write(repo, B, code("LineB", [method(index, name="Check", parameter="input")
                                 for index in range(count)]))
    write(repo, NOISE, code("Noise", [method(123456), method(0, '        return value?.Length > 1;', 'Noise')]))
    save(repo, "branch1: clone A into B and add an unrelated helper")

    git(repo, "switch", "-qc", "branch2", "main")
    write(repo, A, code("LineA", [method(index, NEW) for index in range(count)]))
    write(repo, SIMILAR, code("Similar", [method(399, NEW, name="Validate")]))
    save(repo, "branch2: independently add guards")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "branch2", "-m", "merge branch2")

    if count == 1:
        git(repo, "switch", "-qc", "branch1-fixed", "branch1")
        write(repo, B, code("LineB", [method(0, NEW, name="Check", parameter="input")]))
        save(repo, "branch1: port the fix to B")

        git(repo, "switch", "-qc", "branch1-diverged", "branch1")
        body = OLD.replace('StartsWith("item")', 'StartsWith("assembly")')
        write(repo, B, code("LineB", [method(0, body, name="Check", parameter="input")]))
        save(repo, "branch1: adapt B to the line")

        git(repo, "switch", "-qc", "branch1-existing-file", "clone-point")
        write(repo, NOISE, code("Noise", [method(123456), method(0, name="Check", parameter="input")]))
        save(repo, "branch1: copy A into an existing file")

        git(repo, "switch", "-qc", "branch1-rebased", "branch1")
        git(repo, "rebase", "main")

        git(repo, "switch", "-q", "main")


def check(repo, target, head, base=None):
    command = [sys.executable, "-X", "utf8", str(CHECKER), "--repo", str(repo),
               "--target", target, "--head", head, "--stats"]
    if base:
        command.extend(["--base", base])
    result = subprocess.run(command, capture_output=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout, result.stderr.strip()


def verify(repo):
    cases = [
        ("main", "branch1", None, "反映漏れ候補", B),
        ("clone-point", "branch1", None, "", ""),
        ("main", "branch1-fixed", None, "", ""),
        ("main", "branch1-diverged", None, "要確認", B),
        ("main", "branch1-existing-file", None, "反映漏れ候補", NOISE),
        ("main", "branch1-rebased", "clone-point", "反映漏れ候補", B),
        ("main", "branch2", None, "", ""),
    ]
    for target, head, base, expected, path in cases:
        report, stats = check(repo, target, head, base)
        if expected:
            if (expected not in report or path not in report or A not in report
                    or report.count("\n- **") != 1):
                raise RuntimeError(f"{target}/{head}: {report}")
            if (SIMILAR in report) != (head == "branch1-diverged"):
                raise RuntimeError(f"{target}/{head}: unexpected similar-code candidates")
        elif report:
            raise RuntimeError(f"{target}/{head}: {report}")
        print(f"PASS {target} <- {head}: {expected or '指摘なし'} / {stats}")
        if head == "branch1" and target == "main":
            print(report.rstrip())


def benchmark(repo, count, noise_files, iterations):
    report, stats = check(repo, "main", "branch1")
    if report.count("反映漏れ候補") + report.count("ほか") == 0:
        raise RuntimeError("Expected clone findings.")
    if f"matched_methods={count} " not in stats:
        raise RuntimeError(f"Expected {count} matching methods: {stats}")
    if "target_files=2 source_files=2" not in stats:
        raise RuntimeError(f"Unexpected files inspected: {stats}")

    elapsed = []
    for _ in range(iterations):
        start = perf_counter()
        check(repo, "main", "branch1")
        elapsed.append((perf_counter() - start) * 1_000)
    print(f"{count:>5} clones / {noise_files:>5} unrelated files / "
          f"median {median(elapsed):7.1f} ms / min {min(elapsed):7.1f} ms "
          f"/ max {max(elapsed):7.1f} ms / {stats}")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--sizes", nargs="+", type=int, default=[1, 100, 1000])
    cli.add_argument("--noise-files", type=int, default=5000)
    cli.add_argument("--iterations", type=int, default=10)
    args = cli.parse_args()
    if min(args.sizes) < 1 or args.noise_files < 0 or args.iterations < 1:
        cli.error("Use positive sizes and iterations and a nonnegative noise-file count.")
    print(f"{platform.system()} {platform.machine()} / Python {platform.python_version()}")
    for count in dict.fromkeys([1, *args.sizes]):
        with TemporaryDirectory(prefix="clone-check-") as directory:
            repo = Path(directory)
            fixture(repo, count, args.noise_files)
            if count == 1:
                print(git(repo, "--version"))
                verify(repo)
            if count in args.sizes:
                benchmark(repo, count, args.noise_files, args.iterations)


if __name__ == "__main__":
    sys.exit(main())
