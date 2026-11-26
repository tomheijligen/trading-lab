"""Screen tracked working-tree files without printing matching values."""
import subprocess


PATTERNS = (
    r"-----BEGIN (RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}",
    r"(AKIA|ASIA)[A-Z0-9]{16}|sk_live_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}",
)


def main():
    tracked = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    local_configs = [
        path for path in tracked
        if any(part == ".env" or (part.startswith(".env.") and part != ".env.example")
               for part in path.split("/"))
    ]
    scan = subprocess.run(
        ["git", "grep", "-I", "-l", "-E", "-e", "|".join(PATTERNS), "--",
         ".", ":(exclude)tools/scan_secrets.py"],
        capture_output=True, text=True,
    )
    if scan.returncode not in (0, 1):
        raise SystemExit("Secret scan failed to read the tracked files.")
    findings = sorted(set(local_configs + scan.stdout.splitlines()))
    if findings:
        print("Review tracked files:", *findings, sep="\n")
        return 1
    print("No matches for the configured credential patterns in tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
