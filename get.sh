#!/usr/bin/env bash
# Install or update whisper-dictation on Linux in one line:
#   curl -fsSL https://raw.githubusercontent.com/plastic-eater/whisper-dictation/main/get.sh | bash
# Clones the repo into ~/.local/share/whisper-dictation/src (or updates that
# clone), checks out the latest release, and runs its install.sh.

set -euo pipefail

# Everything is inside main, called on the last line, so a download cut short
# runs nothing.
main() {
    local home="${WHISPER_DICT_HOME:-$HOME/.local/share/whisper-dictation}"
    local src="$home/src" tag
    command -v git >/dev/null || { sudo apt-get update && sudo apt-get install -y git; }
    if [ -d "$src/.git" ]; then
        git -C "$src" fetch --quiet --tags --force origin
    else
        mkdir -p "$home"
        git clone --quiet https://github.com/plastic-eater/whisper-dictation "$src"
    fi
    tag=$(git -C "$src" tag -l 'v*' --sort=-v:refname | head -1)
    echo "==> whisper-dictation $tag"
    git -C "$src" -c advice.detachedHead=false checkout --quiet "$tag"
    # Piped from curl, stdin is this script; give install.sh the terminal for its prompts.
    if (: </dev/tty) 2>/dev/null; then
        "$src/install.sh" </dev/tty
    else
        "$src/install.sh"
    fi
}

main "$@"
