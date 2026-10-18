"""Which files a Bash command will write, so the PreToolUse gate can lease them like an Edit.

Covered: redirections (> >> &> >|), tee, sed -i / perl -i, cp / mv / install (destination; mv also its sources),
git checkout -- <paths> / git restore <paths> / git checkout <existing file>, and scripts run with python/node/ruby
-c/-e or from a heredoc: when their code writes files (open(..., 'w'), write_text, writeFile, ...), every path in the
code with a source-file suffix is a target. A script that writes but names no such path is reported as `opaque`.
Not covered (by design, stated in SETUP.md): writes hidden behind variables, eval, or another script file.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*(?:\n|$)", re.S)
WRITES = re.compile(r"open\([^)]*['\"][wax+]b?['\"]|write_text|write_bytes|\.write\(|writeFile|appendFile|"
                    r"shutil\.(copy|move)|os\.(replace|rename)|File\.write|IO\.write")
PATHLIKE = re.compile(r"[\w./\\-]+\.\w{1,5}")
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}
REDIRECTS = {">", ">>", "&>", ">|", "&>>"}
INTERPRETERS = {"python", "python3", "py", "node", "ruby", "perl", "deno", "bun"}


def _tokens(line: str) -> list[str]:
    lx = shlex.shlex(line, posix=True, punctuation_chars=True)
    lx.whitespace_split = True
    return list(lx)


def _commands(tokens: list[str]) -> list[list[str]]:
    out, cur = [], []
    for t in tokens:
        if t in SEPARATORS:
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    return [c for c in out + [cur] if c]


def _positional(args: list[str], takes_value=()) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in takes_value:
            skip = True
        elif not a.startswith("-") or a == "-":
            out.append(a)
    return out


def _script_paths(code: str) -> tuple[list[str], bool]:
    if not WRITES.search(code):
        return [], False
    paths = sorted({m.group(0) for m in PATHLIKE.finditer(code) if not m.group(0)[0].isdigit()})
    return paths, not paths


def write_targets(cmd: str, cwd: Path | None = None) -> tuple[list[str], bool]:
    """(paths the command writes, as written in it; opaque: it writes files we could not name)."""
    cwd = cwd or Path(".")
    bodies = [m.group(3) for m in HEREDOC.finditer(cmd)]
    cmd = HEREDOC.sub("<<HEREDOC\n", cmd)
    targets, opaque = [], False
    for line in cmd.splitlines():
        try:
            toks = _tokens(line)
        except ValueError:  # unbalanced quotes: we cannot parse it, so we cannot clear it
            return targets, True
        for argv in _commands(toks):
            words, i = [], 0
            while i < len(argv):  # pull out redirections first
                t = argv[i]
                if t in REDIRECTS and i + 1 < len(argv):
                    dst = argv[i + 1]
                    if not dst.startswith("&") and dst not in ("/dev/null", "nul", "NUL"):
                        targets.append(dst)
                    i += 2
                    continue
                if t in ("<", "<<", "<<<", ">&", "<&") and i + 1 < len(argv):
                    i += 2
                    continue
                words.append(t)
                i += 1
            while words and re.match(r"^\w+=", words[0]):  # FOO=bar cmd
                words = words[1:]
            while words and words[0] in ("sudo", "command", "env", "time", "nohup", "xargs"):
                words = words[1:]
            if not words:
                continue
            prog, args = Path(words[0]).name.lower().removesuffix(".exe"), words[1:]
            if prog == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in args):
                given = any(a in ("-e", "-f", "--expression", "--file") for a in args)
                pos = _positional(args, ("-e", "-f", "--expression", "--file"))
                targets += pos if given else pos[1:]
            elif prog == "perl" and any(re.match(r"^-\w*i", a) for a in args):
                targets += [a for a in args if not a.startswith("-")][1:]  # first is the script (-e or a file)
            elif prog == "tee":
                targets += _positional(args)
            elif prog in ("cp", "mv", "install"):
                pos = _positional(args, ("-t", "--target-directory", "-m", "--mode", "-S", "--suffix"))
                if len(pos) >= 2:
                    *srcs, dst = pos
                    d = cwd / dst
                    targets += [str(Path(dst) / Path(s).name) for s in srcs] if d.is_dir() else [dst]
                    if prog == "mv":
                        targets += srcs
            elif prog == "git" and args[:1] in (["checkout"], ["restore"]):
                rest = args[1:]
                if "--" in rest:
                    targets += [a for a in rest[rest.index("--") + 1:]]
                elif args[0] == "restore":
                    targets += _positional(rest, ("-s", "--source"))
                else:  # `git checkout <file>` restores it; `git checkout <branch>` doesn't name files
                    targets += [a for a in _positional(rest, ("-b", "-B")) if (cwd / a).is_file()]
            elif prog.rstrip("0123456789.") in INTERPRETERS or prog in INTERPRETERS:
                code = next((args[k + 1] for k, a in enumerate(args) if a in ("-c", "-e") and k + 1 < len(args)), None)
                if code is None and ("-" in args or "<<HEREDOC" in line):
                    code = "\n".join(bodies)
                if code:
                    paths, opq = _script_paths(code)
                    targets += paths
                    opaque = opaque or opq
    return list(dict.fromkeys(targets)), opaque


if __name__ == "__main__":
    import sys
    print(write_targets(sys.argv[1]))
