import pytest

from app.risk import Risk, classify


@pytest.mark.parametrize("cmd", [
    "df -h",
    "free -m",
    "uptime",
    "ps aux | grep nginx | head -20",
    "tail -n 200 /var/log/nginx/error.log",
    "journalctl -u nginx --since '1 hour ago' --no-pager",
    "systemctl status nginx",
    "docker ps -a",
    "docker logs --tail 100 web",
    "kubectl get pods -n prod",
    "du -sh /var/* 2>/dev/null | sort -h",
    "cat /etc/os-release && uname -a",
    "ss -lntp",
    "grep -i error /var/log/syslog | wc -l",
    "find /var/log -name '*.gz' -mtime +7",
    "sudo tail -f /var/log/messages",
    "curl -sI http://localhost:8080/health",
    "ls -la /opt/app; echo done",
    "top -b -n 1",
    "git -C /opt/app log --oneline -5",
    "awk '{print $1}' access.log | sort | uniq -c | sort -rn | head",
    "sed -n '1,50p' /etc/nginx/nginx.conf",
    "lsof -i :80",
    "ip addr",
    "env",
    "watch -n 1 df -h",
    "env LANG=C df -h",
    "tcpdump -i eth0 -c 10 port 80",
])
def test_read_only_commands_are_safe(cmd):
    assert classify(cmd).level == Risk.SAFE, classify(cmd).reason


@pytest.mark.parametrize("cmd", [
    "rm /var/log/nginx/access.log.1.gz",
    "systemctl restart nginx",
    "docker restart web",
    "kubectl delete pod web-1 -n prod",
    "echo hello > /tmp/a.txt",
    "cat a >> b",
    "sed -i 's/a/b/' /etc/hosts",
    "find /var/log -name '*.gz' -delete",
    "find / -name x -exec rm {} \\;",
    "kill -9 1234",
    "reboot",
    "yum install -y htop",
    "apt-get update",
    "chmod 600 /root/.ssh/id_rsa",
    "mv a b",
    "ls $(rm -rf /tmp/x)",
    "ls `whoami`",
    "curl -X POST http://localhost/api",
    "curl -o /tmp/f http://x",
    "wget http://example.com/a.tar.gz",
    "tee /etc/motd",
    "some_custom_script.sh",
    "bash -c 'echo hi'",
    "python3 -c 'print(1)'",
    "crontab -e",
    "iptables -F",
    "git -C /opt/app pull",
    "ip link set eth0 down",
    "truncate -s 0 /var/log/big.log",
    "env rm -rf /tmp/x",
    "env FOO=1 systemctl restart nginx",
    "watch rm x",
    "watch -n 2 'kill 1'",
    "sudo -i",
    "sudo su -",
    "tcpdump -w /tmp/a.pcap",
    "ls | xargs -I{} rm {}",
    "nice -n 10 rm a",
])
def test_mutating_or_unknown_commands_need_approval(cmd):
    assert classify(cmd).level == Risk.APPROVE, classify(cmd).reason


@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf --no-preserve-root /",
    "sudo rm -fr / ",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    ":(){ :|:& };:",
    "echo x > /dev/sda",
    "chmod -R 777 /",
    "env rm -rf /",
    "watch 'rm -rf /'",
    "ls; rm -rf /",
    "ls\nrm -rf /",
])
def test_catastrophic_commands_are_forbidden(cmd):
    assert classify(cmd).level == Risk.FORBIDDEN, classify(cmd).reason


def test_empty_command_forbidden():
    assert classify("   ").level == Risk.FORBIDDEN


def test_unparseable_command_needs_approval():
    assert classify("echo 'unterminated").level == Risk.APPROVE


def test_stderr_redirect_to_devnull_is_still_safe():
    assert classify("ls /nope 2>/dev/null").level == Risk.SAFE
    assert classify("ls /nope 2>&1").level == Risk.SAFE
