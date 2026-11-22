"""Container entrypoint: drop from root to PUID:PGID (Unraid's nobody:users, 99:100, by default).

Fixes ownership of the data folder first, so data written by older root-run versions keeps working.
"""
import os
import sys

if os.getuid() == 0 and os.environ.get("PUID", "99") != "0":
    uid, gid = int(os.environ.get("PUID", "99")), int(os.environ.get("PGID", "100"))
    os.umask(int(os.environ.get("UMASK", "022"), 8))
    data = os.environ.get("DATA_DIR", "/data")
    os.makedirs(data, exist_ok=True)
    for root, dirs, files in os.walk(data):
        for p in [root] + [os.path.join(root, n) for n in files]:
            try:
                st = os.lstat(p)
                if st.st_uid != uid or st.st_gid != gid:
                    os.lchown(p, uid, gid)
            except OSError as e:
                print(f"warning: couldn't change owner of {p}: {e}", file=sys.stderr)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = data

os.execvp(sys.argv[1], sys.argv[1:])
