"""命令风险分级。

三个等级：
- SAFE       只读命令，agent 可直接执行
- APPROVE    会改动系统或无法判断，必须由人在网页上点「批准」
- FORBIDDEN  灾难性命令（rm -rf /、mkfs、写裸盘、fork 炸弹…），即使批准也不执行

采用「白名单」思路：只有能确定是只读的命令才判为 SAFE，其余一律需要审批。
"""
import re
import shlex
from dataclasses import dataclass
from enum import Enum


class Risk(str, Enum):
    SAFE = "safe"
    APPROVE = "approve"
    FORBIDDEN = "forbidden"


@dataclass
class Classification:
    level: Risk
    reason: str


def _safe() -> Classification:
    return Classification(Risk.SAFE, "只读命令")


def _approve(reason: str) -> Classification:
    return Classification(Risk.APPROVE, reason)


def _forbid(reason: str) -> Classification:
    return Classification(Risk.FORBIDDEN, reason)


# 无副作用、可直接执行的命令（参数再做细查的命令在下面单独处理）
SAFE_COMMANDS = {
    "df", "free", "uptime", "ps", "grep", "egrep", "fgrep", "zgrep", "head", "tail", "cat", "zcat", "tac",
    "ls", "ll", "stat", "file", "which", "whereis", "whoami", "id", "hostname", "uname", "date", "pwd",
    "printenv", "echo", "printf", "lsblk", "blkid", "findmnt", "ss", "netstat", "ping", "traceroute",
    "tracepath", "dig", "nslookup", "host", "top", "htop", "vmstat", "iostat", "mpstat", "sar", "pidstat",
    "lsof", "nproc", "lscpu", "lsmem", "lspci", "lsusb", "last", "lastb", "w", "who", "getent", "du",
    "uniq", "wc", "cut", "nl", "strings", "md5sum", "sha1sum", "sha256sum", "readlink", "realpath",
    "basename", "dirname", "test", "true", "false", "tree", "jq", "column", "tr", "diff", "cmp", "ldd",
    "pgrep", "pstree", "iotop", "iftop", "nethogs", "ulimit", "locale", "arch", "env", "type", "base64",
    "watch", "less", "more", "sleep", "seq", "xxd", "od", "hexdump", "pmap", "numactl", "lsmod",
    "modinfo", "ethtool", "arp", "route", "nload", "dstat", "glances",
}

FORBIDDEN_SYSTEM_PATHS = {
    "/", "/*", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/proc", "/root", "/sbin",
    "/sys", "/usr", "/var",
}
RAW_DISK_RE = re.compile(r"^/dev/(sd[a-z]|hd[a-z]|vd[a-z]|xvd[a-z]|nvme\d|mmcblk\d|dm-\d|mapper/)")
FORK_BOMB_RE = re.compile(r":\s*\(\s*\)\s*\{.*:\s*\|\s*:.*\}")

OPERATOR_CHARS = set("();<>|&")


def classify(command: str) -> Classification:
    cmd = command.strip()
    if not cmd:
        return _forbid("空命令")
    if FORK_BOMB_RE.search(cmd):
        return _forbid("fork 炸弹")

    # 换行等价于 ;，否则 shlex 会把它当空白吞掉
    normalized = cmd.replace("\r", " ").replace("\n", " ; ")
    try:
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return _approve("命令无法解析（引号不匹配？）")

    segments, verdict = _split_segments(tokens)
    if verdict and verdict.level == Risk.FORBIDDEN:
        return verdict

    # 先查所有片段有没有禁止项，再看是否需要审批
    results = [_classify_segment(seg) for seg in segments if seg]
    for r in results:
        if r.level == Risk.FORBIDDEN:
            return r

    if "$(" in cmd or "`" in cmd:
        return _approve("包含命令替换 $() 或反引号，无法静态判断")
    if verdict:
        return verdict
    for r in results:
        if r.level == Risk.APPROVE:
            return r
    return _safe()


def _split_segments(tokens: list[str]) -> tuple[list[list[str]], Classification | None]:
    """按管道/分隔符切分成多个简单命令，同时检查重定向。"""
    segments: list[list[str]] = [[]]
    verdict: Classification | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok and all(ch in OPERATOR_CHARS for ch in tok):
            if ">" in tok:
                target = tokens[i + 1] if i + 1 < len(tokens) else ""
                if tok.endswith("&") or tok == ">&":
                    # 2>&1 之类，目标是文件描述符
                    if target.isdigit() or target == "-":
                        i += 2
                        continue
                if RAW_DISK_RE.match(target):
                    return segments, _forbid(f"向裸磁盘 {target} 写入")
                if target != "/dev/null" and verdict is None:
                    verdict = _approve(f"重定向写入文件 {target}")
                i += 2
                continue
            if "<<" in tok:
                verdict = verdict or _approve("包含 heredoc，无法静态判断")
                i += 1
                continue
            if tok == "<":
                i += 2  # 输入重定向：跳过文件名
                continue
            segments.append([])
            i += 1
            continue
        segments[-1].append(tok)
        i += 1
    return segments, verdict


def _strip_prefixes(argv: list[str]) -> list[str]:
    """去掉 sudo / 环境变量赋值 / timeout / nice 等前缀，得到真正执行的命令。"""
    changed = True
    while argv and changed:
        changed = False
        head = argv[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            argv = argv[1:]
            changed = True
        elif head == "sudo":
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                flag = argv[0]
                if flag in ("-i", "-s", "--login", "--shell", "-e", "--edit"):
                    return ["sudo", flag]  # 交给 SPECIAL_CHECKERS["sudo"] 判为需审批
                argv = argv[1:]
                if flag in ("-u", "-g", "-U", "-C", "-h", "-p") and argv:
                    argv = argv[1:]
            changed = True
        elif head == "env":
            rest = argv[1:]
            while rest and (rest[0].startswith("-") or "=" in rest[0]):
                flag = rest[0]
                rest = rest[1:]
                if flag in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string") and rest:
                    rest = rest[1:]
            if not rest:
                return ["env"]
            argv = rest
            changed = True
        elif head == "watch":
            rest = argv[1:]
            while rest and rest[0].startswith("-"):
                flag = rest[0]
                rest = rest[1:]
                if flag in ("-n", "--interval", "-d", "--differences") and rest and re.match(r"^[\d.]+$", rest[0]):
                    rest = rest[1:]
            if not rest:
                return ["watch"]
            # watch 会把参数拼成一条 shell 命令执行，这里递归判断
            return ["__shell__", " ".join(rest)]
        elif head in ("timeout", "nice", "ionice", "nohup", "stdbuf", "time"):
            argv = argv[1:]
            while argv and (argv[0].startswith("-") or re.match(r"^\d+[smhd]?$", argv[0])):
                argv = argv[1:]
            changed = True
    return argv


def _classify_segment(argv: list[str]) -> Classification:
    argv = _strip_prefixes(argv)
    if not argv:
        return _safe()
    name = argv[0].rsplit("/", 1)[-1]
    args = argv[1:]

    forbidden = _check_forbidden(name, args)
    if forbidden:
        return forbidden

    if name == "__shell__":
        return classify(args[0])

    checker = SPECIAL_CHECKERS.get(name)
    if checker:
        return checker(args)
    if name in SAFE_COMMANDS:
        return _safe()
    return _approve(f"`{name}` 不在只读白名单中")


def _check_forbidden(name: str, args: list[str]) -> Classification | None:
    if name.startswith("mkfs") or name in ("mkswap", "wipefs", "fdisk", "sfdisk", "parted"):
        if name in ("fdisk", "sfdisk", "parted") and any(a in ("-l", "--list") for a in args):
            return None
        return _forbid(f"`{name}` 会破坏磁盘分区/文件系统")
    if "--no-preserve-root" in args:
        return _forbid("使用了 --no-preserve-root")
    if name == "rm":
        recursive = any(
            a in ("--recursive",) or (a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a))
            for a in args
        )
        targets = [a.rstrip("/") or "/" for a in args if not a.startswith("-")]
        if recursive and any(t in FORBIDDEN_SYSTEM_PATHS or t in ("~", "/*", "*") for t in targets):
            return _forbid("递归删除系统关键目录")
    if name in ("chmod", "chown", "chgrp"):
        recursive = any(a in ("-R", "--recursive") or (a.startswith("-") and "R" in a) for a in args)
        targets = [a.rstrip("/") or "/" for a in args if not a.startswith("-")]
        if recursive and any(t in FORBIDDEN_SYSTEM_PATHS for t in targets):
            return _forbid(f"递归修改系统目录权限（{name} -R）")
    if name == "dd":
        for a in args:
            if a.startswith("of=") and RAW_DISK_RE.match(a[3:]):
                return _forbid("dd 写入裸磁盘")
    if name == "shred" and any(RAW_DISK_RE.match(a) for a in args):
        return _forbid("shred 擦除裸磁盘")
    return None


# ---------------- 需要看参数才能判断的命令 ----------------

def _first_verb(args: list[str], flags_with_value: set[str] = frozenset()) -> tuple[str | None, list[str]]:
    i = 0
    while i < len(args):
        a = args[i]
        if a in flags_with_value:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1:]
    return None, []


def _check_systemctl(args):
    verb, _ = _first_verb(args, {"-H", "--host", "-M", "--machine", "-t", "--type", "--state", "-p", "--property"})
    safe = {"status", "show", "list-units", "list-unit-files", "list-timers", "list-sockets", "list-jobs",
            "is-active", "is-enabled", "is-failed", "cat", "list-dependencies", "get-default", "help"}
    if verb is None or verb in safe:
        return _safe()
    return _approve(f"systemctl {verb} 会改变服务状态")


def _check_journalctl(args):
    for a in args:
        if a.startswith(("--vacuum", "--rotate", "--flush", "--sync", "--relinquish", "--setup-keys")):
            return _approve(f"journalctl {a} 会修改日志")
    return _safe()


def _check_docker(args):
    verb, rest = _first_verb(args, {"-H", "--host", "--context", "-c", "--config", "-l", "--log-level"})
    safe = {"ps", "logs", "inspect", "images", "top", "version", "info", "port", "diff", "history", "events"}
    if verb in safe:
        return _safe()
    if verb == "stats" and "--no-stream" in rest:
        return _safe()
    if verb in ("network", "volume", "image", "container", "node", "service", "system", "context"):
        sub, _ = _first_verb(rest)
        if sub in ("ls", "list", "inspect", "logs", "ps", "df", "show"):
            return _safe()
    if verb == "compose":
        sub, _ = _first_verb(rest, {"-f", "--file", "-p", "--project-name", "--env-file", "--profile"})
        if sub in ("ps", "logs", "config", "images", "top", "ls", "version"):
            return _safe()
    return _approve(f"docker {verb or ''} 可能改变容器状态".strip())


def _check_docker_compose(args):
    return _check_docker(["compose", *args])


def _check_kubectl(args):
    verb, rest = _first_verb(args, {"-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user",
                                    "-s", "--server", "-l", "--selector", "-o", "--output", "-c", "--container"})
    safe = {"get", "describe", "logs", "top", "version", "explain", "api-resources", "api-versions",
            "cluster-info", "events"}
    if verb in safe:
        return _safe()
    sub, _ = _first_verb(rest, {"-n", "--namespace"})
    if verb == "config" and sub in ("view", "get-contexts", "current-context", "get-clusters"):
        return _safe()
    if verb == "rollout" and sub in ("status", "history"):
        return _safe()
    if verb == "auth" and sub == "can-i":
        return _safe()
    return _approve(f"kubectl {verb or ''} 可能改变集群状态".strip())


def _check_git(args):
    verb, rest = _first_verb(args, {"-C", "-c", "--git-dir", "--work-tree"})
    safe = {"log", "status", "diff", "show", "rev-parse", "describe", "ls-files", "blame", "shortlog",
            "reflog", "ls-remote", "rev-list", "cat-file", "grep"}
    if verb in safe:
        return _safe()
    if verb == "branch" and all(a.startswith("-") and a not in ("-d", "-D", "-m", "-M", "-c", "-C", "-f",
                                                                  "--delete", "--move", "--copy", "--force")
                                for a in rest):
        return _safe()
    if verb in ("remote", "tag") and all(a in ("-v", "--verbose", "-l", "--list", "show") or not rest
                                        for a in rest[:1]):
        if not rest or rest[0] in ("-v", "--verbose", "-l", "--list"):
            return _safe()
    if verb == "config" and any(a in ("--get", "--list", "-l", "--get-all") for a in rest):
        return _safe()
    return _approve(f"git {verb or ''} 可能修改仓库".strip())


def _check_find(args):
    bad = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
    for a in args:
        if a in bad:
            return _approve(f"find {a} 会执行操作或写文件")
    return _safe()


def _check_sed(args):
    for a in args:
        if a.startswith("-i") or a.startswith("--in-place") or (a.startswith("-") and not a.startswith("--")
                                                               and "i" in a[1:] and len(a) <= 4):
            return _approve("sed -i 会原地修改文件")
    for a in args:
        if re.search(r"(^|[;}\s])[wWe]\s+\S", a):
            return _approve("sed 脚本包含写文件/执行命令")
    return _safe()


def _check_awk(args):
    for a in args:
        if "system" in a or ">" in a or "|" in a or "getline" in a:
            return _approve("awk 脚本可能执行命令或写文件")
    return _safe()


def _check_sort(args):
    if any(a == "-o" or a.startswith("--output") or (a.startswith("-o") and len(a) > 2) for a in args):
        return _approve("sort -o 会写文件")
    return _safe()


def _check_curl(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-X", "--request"):
            method = (args[i + 1] if i + 1 < len(args) else "").upper()
            if method not in ("GET", "HEAD"):
                return _approve(f"curl {method} 请求可能有副作用")
            i += 2
            continue
        if a.startswith("-X") and len(a) > 2 and a[2:].upper() not in ("GET", "HEAD"):
            return _approve("curl 非 GET 请求可能有副作用")
        if a in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form", "-T",
                 "--upload-file", "-o", "--output", "-O", "--remote-name", "--json") or a.startswith(
                ("--data", "--output", "--form", "--upload")):
            return _approve(f"curl {a} 会发送数据或写文件")
        i += 1
    return _safe()


def _check_wget(args):
    if "--spider" in args:
        return _safe()
    return _approve("wget 会下载文件")


def _check_ip(args):
    mutating = {"add", "del", "delete", "set", "flush", "change", "replace", "append", "prepend", "up", "down",
                "exec", "attach", "detach"}
    if any(a in mutating for a in args):
        return _approve("ip 命令会修改网络配置")
    return _safe()


def _check_ifconfig(args):
    if len([a for a in args if not a.startswith("-")]) > 1:
        return _approve("ifconfig 带参数可能修改网卡配置")
    return _safe()


def _check_dmesg(args):
    if any(a in ("-c", "-C", "--clear", "--read-clear", "-n", "--console-level", "-D", "-E") for a in args):
        return _approve("dmesg 该参数会修改内核日志")
    return _safe()


def _check_sysctl(args):
    if any(a in ("-w", "--write", "-p", "--load", "--system") or "=" in a for a in args):
        return _approve("sysctl 会修改内核参数")
    return _safe()


def _check_crontab(args):
    if args and all(a in ("-l", "-u") or not a.startswith("-") for a in args) and "-l" in args:
        return _safe()
    return _approve("crontab 会修改计划任务")


def _check_mount(args):
    if not args or args == ["-l"] or args == ["-v"]:
        return _safe()
    return _approve("mount 带参数会挂载文件系统")


def _check_nginx(args):
    if args and all(a in ("-t", "-T", "-v", "-V", "-q") for a in args):
        return _safe()
    return _approve("nginx 该参数会控制服务进程")


def _check_status_only(name):
    def check(args):
        if not args or args[0] in ("status", "show", "list", "--help"):
            return _safe()
        return _approve(f"{name} {args[0]} 会修改系统设置")
    return check


def _check_rpm(args):
    if args and args[0].startswith("-q"):
        return _safe()
    return _approve("rpm 会安装/卸载软件包")


def _check_dpkg(args):
    if args and args[0] in ("-l", "-L", "-s", "-S", "--list", "--listfiles", "--status", "--search", "-p"):
        return _safe()
    return _approve("dpkg 会安装/卸载软件包")


def _check_pkg_manager(name):
    def check(args):
        verb, _ = _first_verb(args)
        if verb in ("list", "info", "search", "show", "history", "repolist", "check-update", "policy",
                    "provides", "whatprovides", "deplist"):
            return _safe()
        return _approve(f"{name} {verb or ''} 会修改软件包".strip())
    return check


def _check_tcpdump(args):
    if any(a == "-w" or (a.startswith("-w") and len(a) > 2) or a == "-z" for a in args):
        return _approve("tcpdump -w/-z 会写文件或执行命令")
    return _safe()


SPECIAL_CHECKERS = {
    "sudo": lambda args: _approve("sudo -i/-s/-e 会打开交互 shell 或编辑文件"),
    "tcpdump": _check_tcpdump,
    "systemctl": _check_systemctl,
    "journalctl": _check_journalctl,
    "docker": _check_docker,
    "docker-compose": _check_docker_compose,
    "podman": _check_docker,
    "kubectl": _check_kubectl,
    "git": _check_git,
    "find": _check_find,
    "sed": _check_sed,
    "awk": _check_awk,
    "gawk": _check_awk,
    "mawk": _check_awk,
    "sort": _check_sort,
    "curl": _check_curl,
    "wget": _check_wget,
    "ip": _check_ip,
    "ifconfig": _check_ifconfig,
    "dmesg": _check_dmesg,
    "sysctl": _check_sysctl,
    "crontab": _check_crontab,
    "mount": _check_mount,
    "nginx": _check_nginx,
    "timedatectl": _check_status_only("timedatectl"),
    "hostnamectl": _check_status_only("hostnamectl"),
    "rpm": _check_rpm,
    "dpkg": _check_dpkg,
    "yum": _check_pkg_manager("yum"),
    "dnf": _check_pkg_manager("dnf"),
    "apt": _check_pkg_manager("apt"),
    "apt-cache": lambda args: _safe(),
}
