"""Generate a patch for pinned upstream sources without modifying the checkout."""
from pathlib import Path
import difflib
import subprocess
import sys

COMMIT = "4337bd85758d54d7b9044b2da3c9ac1176eae1f3"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".pio/research/clash-verge-rev-v2.5.8"
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != COMMIT:
        raise SystemExit("Wrong upstream commit; expected " + COMMIT)
    paths = ["src-tauri/src/utils/mod.rs", "src-tauri/src/utils/server.rs"]
    originals = {p: subprocess.check_output(["git", "-C", str(source), "show", "HEAD:" + p]).decode("utf-8") for p in paths}
    modified = dict(originals)
    modified[paths[0]] += "\npub(crate) mod xte_link_bridge;\npub(crate) mod xte_link_backend;\n"
    anchor = "    let commands = auth.clone().and(visible).or(auth.and(scheme)).or(pac);"
    if modified[paths[1]].count(anchor) != 1:
        raise SystemExit("Upstream server route differs from reviewed source")
    replacement = """    let xte_link = crate::utils::xte_link_bridge::routes(std::sync::Arc::new(
        crate::utils::xte_link_backend::VergeBackend {
            ready: || COMMANDS_READY.load(Ordering::Acquire),
        },
    ));
    let commands = auth.clone().and(xte_link)
        .or(auth.clone().and(visible)).or(auth.and(scheme)).or(pac);"""
    modified[paths[1]] = modified[paths[1]].replace(anchor, replacement)
    for local, remote in [("bridge.rs", "xte_link_bridge.rs"), ("verge_backend.rs", "xte_link_backend.rs")]:
        path = "src-tauri/src/utils/" + remote
        originals[path] = ""
        modified[path] = (HERE / local).read_text(encoding="utf-8")
    output = []
    for path, text in modified.items():
        output.append("diff --git a/" + path + " b/" + path + "\n")
        if not originals[path]:
            output.append("new file mode 100644\n")
        output.extend(difflib.unified_diff(originals[path].splitlines(True), text.splitlines(True),
                                         fromfile="a/" + path if originals[path] else "/dev/null", tofile="b/" + path))
    destination = HERE / "verge-2.5.8.patch"
    destination.write_text("".join(output), encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(source), "apply", "--check", str(destination)], check=True)
    print("Patch generated and checked; upstream checkout was not modified:", destination)


if __name__ == "__main__":
    main()
