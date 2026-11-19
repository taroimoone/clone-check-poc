#!/usr/bin/env python3
"""Find newly copied C# methods that may omit a change from the target branch."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
import subprocess
import sys

from tree_sitter import Language, Parser
import tree_sitter_c_sharp


MIN_TOKENS = 16
MIN_SIMILAR_TOKENS = 24
SHINGLE_SIZE = 5
SIMILARITY = 0.88
TYPE_NODES = {
    "class_declaration", "struct_declaration", "record_declaration",
    "interface_declaration", "namespace_declaration",
}
LITERAL_NODES = {
    "string_literal", "character_literal", "verbatim_string_literal",
    "raw_string_literal", "interpolated_string_expression",
}


@dataclass(frozen=True)
class Method:
    path: str
    line: int
    name: str
    identity: tuple
    tokens: tuple


@dataclass(frozen=True)
class Edit:
    before: Method
    after: Method


def git(repo, *args, data=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout


def commit(repo, ref):
    return git(repo, "rev-parse", "--verify", "--end-of-options",
               ref + "^{commit}").decode().strip()


def changed_files(repo, base, tip, statuses):
    paths = git(repo, "diff", "--no-renames", f"--diff-filter={statuses}",
                "--name-only", "-z", base, tip, "--")
    return [path.decode("utf-8") for path in paths.split(b"\0")
            if path.lower().endswith(b".cs")]


def blobs_at(repo, files):
    keys = list(dict.fromkeys(files))
    if not keys:
        return {}
    if any("\n" in path or "\r" in path for _, path in keys):
        raise ValueError("File paths containing line breaks are unsupported.")

    request = "".join(f"{tree}:{path}\n" for tree, path in keys)
    output = BytesIO(git(repo, "cat-file", "--batch", data=request.encode("utf-8")))
    blobs = {}
    for key in keys:
        header = output.readline().rstrip(b"\n")
        if header.endswith(b" missing"):
            blobs[key] = None
            continue
        _, kind, size = header.split()
        content = output.read(int(size))
        if output.read(1) != b"\n":
            raise ValueError("Incomplete Git object response.")
        blobs[key] = content if kind == b"blob" else None
    return blobs


def tokens(node, source, parameters):
    if node.type == "comment":
        return
    if node.type in LITERAL_NODES or not node.children:
        raw = source[node.start_byte:node.end_byte]
        if node.type == "identifier" and raw in parameters:
            parent = node.parent
            qualified = parent.type in {"member_access_expression", "member_binding_expression"}
            if not (qualified and parent.child_by_field_name("name") == node):
                yield parameters[raw]
                return
        yield raw
        return
    for child in node.children:
        yield from tokens(child, source, parameters)


def name_of(node, source):
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8")


def method_info(node, source, path, scope):
    name = name_of(node.child_by_field_name("name"), source)
    types = []
    names = {}
    for param in node.child_by_field_name("parameters").named_children:
        name_node = param.child_by_field_name("name")
        if name_node is None:
            continue
        # Keep each parameter's modifiers and type in the identity used across revisions.
        types.append(tuple(
            token for child in param.children
            if child.end_byte <= name_node.start_byte and child.type != "attribute_list"
            for token in tokens(child, source, {})
        ))
        param_name = name_of(name_node, source).encode("utf-8")
        names[param_name] = f"#arg{len(names)}".encode()
    generics = next((child for child in node.named_children
                     if child.type == "type_parameter_list"), None)
    arity = sum(child.type == "type_parameter" for child in generics.named_children) if generics else 0
    interface = next((child for child in node.named_children
                      if child.type == "explicit_interface_specifier"), None)
    identity = (scope, name_of(interface, source), name, arity, tuple(types))
    body = node.child_by_field_name("body")
    if body is None or node.has_error:
        return None
    return Method(path, node.start_point.row + 1, name, identity, tuple(tokens(body, source, names)))


def parse_methods(blob, path, parser):
    if blob is None:
        return {}, False
    if blob.startswith((b"\xff\xfe", b"\xfe\xff")):
        source = blob.decode("utf-16").encode("utf-8")
    else:
        source = blob.decode("utf-8-sig").encode("utf-8")
    root = parser.parse(source).root_node
    file_scope = next((child for child in root.named_children
                       if child.type == "file_scoped_namespace_declaration"), None)
    scope = (name_of(file_scope.child_by_field_name("name"), source),) if file_scope else ()
    pending = [(root, scope)]
    methods = {}
    duplicate = set()
    while pending:
        node, scope = pending.pop()
        if node.type == "method_declaration":
            method = method_info(node, source, path, scope)
            if method is not None:
                if method.identity in methods:
                    duplicate.add(method.identity)
                methods[method.identity] = method
            continue
        if node.type in TYPE_NODES:
            scope += (name_of(node.child_by_field_name("name"), source),)
        pending.extend((child, scope) for child in reversed(node.named_children))
    for identity in duplicate:
        del methods[identity]
    return methods, bool(root.has_error or duplicate)


def read_methods(repo, files, parser, cache, skipped):
    missing = set(files) - cache.keys()
    for (tree, path), blob in blobs_at(repo, sorted(missing)).items():
        try:
            cache[tree, path], incomplete = parse_methods(blob, path, parser)
            if incomplete:
                skipped.add(path)
        except UnicodeError:
            cache[tree, path] = {}
            skipped.add(path)


def shingles(sequence):
    return {sequence[i:i + SHINGLE_SIZE] for i in range(len(sequence) - SHINGLE_SIZE + 1)}


class CloneIndex:
    def __init__(self, edits):
        self.exact = defaultdict(list)
        self.fixed = defaultdict(list)
        self.near = defaultdict(list)
        self.edits = edits
        for index, edit in enumerate(edits):
            body = edit.before.tokens
            self.exact[body].append(index)
            self.fixed[edit.after.tokens].append(index)
            if len(body) >= MIN_SIMILAR_TOKENS:
                for shingle in shingles(body):
                    self.near[shingle].append(index)

    def match(self, body):
        exact = self.exact.get(body, [])
        fixed = self.fixed.get(body, [])
        if exact:
            return exact, not fixed
        if fixed or len(body) < MIN_SIMILAR_TOKENS:
            return [], False
        hits = Counter(
            index for shingle in sorted(shingles(body))
            if 0 < len(bucket := self.near.get(shingle, ())) <= 64
            for index in bucket
        )
        matches = []
        for index, count in sorted(hits.items(), key=lambda item: (-item[1], item[0]))[:32]:
            before = self.edits[index].before.tokens
            if count < 2 or 2 * min(len(before), len(body)) / (len(before) + len(body)) < SIMILARITY:
                continue
            similarity = SequenceMatcher(None, before, body, autojunk=False).ratio()
            if similarity >= SIMILARITY:
                matches.append(index)
        return matches, False


def check(repo, target, head, base):
    targets = changed_files(repo, base, target, "MT")
    sources = changed_files(repo, base, head, "AMT")
    stats = {
        "target_files": len(targets), "source_files": len(sources),
        "edited_methods": 0, "added_methods": 0, "matched_methods": 0, "merge": False,
    }
    skipped = set()
    if not targets or not sources:
        return [], stats, skipped

    parser = Parser(Language(tree_sitter_c_sharp.language()))
    cache = {}
    read_methods(repo, ((tree, path) for path in targets for tree in (base, target)),
                 parser, cache, skipped)
    edits = [
        Edit(before, after)
        for path in targets for key, before in cache[base, path].items()
        if len(before.tokens) >= MIN_TOKENS
        and (after := cache[target, path].get(key)) is not None and before.tokens != after.tokens
    ]
    stats["edited_methods"] = len(edits)
    if not edits:
        return [], stats, skipped
    index = CloneIndex(edits)

    read_methods(repo, ((tree, path) for path in sources for tree in (base, head)),
                 parser, cache, skipped)
    pending = []
    for path in sources:
        for key, method in cache[head, path].items():
            if key in cache[base, path] or len(method.tokens) < MIN_TOKENS:
                continue
            stats["added_methods"] += 1
            matches, _ = index.match(method.tokens)
            if matches:
                pending.append(method)
    stats["matched_methods"] = len(pending)
    if not pending:
        return [], stats, skipped

    merged = git(repo, "merge-tree", "--write-tree", target, head).decode().strip()
    stats["merge"] = True
    read_methods(repo, (
        (merged, path) for path in {method.path for method in pending} | set(targets)
    ), parser, cache, skipped)
    findings = []
    for method in pending:
        after = cache[merged, method.path].get(method.identity)
        if after is None:
            findings.append(("比較不能", method, []))
            continue
        matches, exact = index.match(after.tokens)
        present = []
        incomplete = []
        for item in matches:
            edit = edits[item]
            merged_a = cache[merged, edit.after.path].get(edit.after.identity)
            if merged_a is None:
                incomplete.append(edit)
            elif merged_a.tokens != edit.before.tokens and merged_a.tokens != after.tokens:
                present.append(edit)
        if incomplete:
            findings.append(("比較不能", after, incomplete))
            continue
        if present:
            confirmed = (exact and len(matches) == 1 and
                         cache[merged, present[0].after.path][present[0].after.identity].tokens
                         == present[0].after.tokens)
            findings.append(("反映漏れ候補" if confirmed else "要確認", after, present))
    return findings, stats, skipped


def location(method):
    path = method.path.replace("`", "'")
    return f"`{path}:{method.line}` の `{method.name}`"


def show(findings, stats, skipped, show_stats):
    if show_stats:
        summary = " ".join(f"{name}={value}" for name, value in stats.items())
        print(summary, file=sys.stderr)
    if skipped:
        paths = ", ".join(sorted(skipped)[:8])
        print(f"一部のメソッドを解析できなかったファイル {len(skipped)} 件: {paths}", file=sys.stderr)
    if findings:
        print("### クローン先への変更反映を確認\n")
        for kind, method, edits in findings[:50]:
            matches = "、".join(location(edit.after) for edit in edits[:3])
            remaining = f" ほか{len(edits) - 3}件" if len(edits) > 3 else ""
            evidence = f"。main側のコピー元候補 {matches}{remaining}" if edits else ""
            if kind == "反映漏れ候補":
                evidence += "。仮マージ後もBはコピー元の修正前のコードと一致します"
            print(f"- **{kind}** — {location(method)}{evidence}。")
        if len(findings) > 50:
            print(f"\nほか{len(findings) - 50}件。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Target branch or commit")
    parser.add_argument("--head", default="HEAD", help="Source branch or commit")
    parser.add_argument("--base", help="Original common ancestor, if branch history was rewritten")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--stats", action="store_true", help="Emit candidate counts to stderr")
    args = parser.parse_args()
    try:
        target, head = (commit(args.repo, ref) for ref in (args.target, args.head))
        bases = git(args.repo, "merge-base", "--all", target, head).decode().split()
        if args.base:
            base = commit(args.repo, args.base)
            git(args.repo, "merge-base", "--is-ancestor", base, target)
            git(args.repo, "merge-base", "--is-ancestor", base, head)
        else:
            if len(bases) != 1:
                raise ValueError("Cannot choose one merge base. Pass the original base with --base.")
            base = bases[0]
        findings, stats, skipped = check(args.repo, target, head, base)
    except subprocess.CalledProcessError as error:
        if "merge-tree" in error.cmd and error.returncode == 1:
            print("仮マージにコンフリクトがあり、判定できません。", file=sys.stderr)
        else:
            print(error.stderr.decode(errors="replace").strip() or "Git comparison failed.",
                  file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"Clone check failed: {error}", file=sys.stderr)
        return 2

    show(findings, stats, skipped, args.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
