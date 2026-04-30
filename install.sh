#!/usr/bin/env bash
# Chinese OSINT — 跨平台一键安装脚本
#
# 用法:
#   ./install.sh all          # 全部平台
#   ./install.sh hermes       # 仅 Hermes Agent
#   ./install.sh claude       # 仅 Claude Code
#   ./install.sh opencode     # 仅 OpenCode
#   ./install.sh cursor       # 仅 Cursor
#   ./install.sh windsurf     # 仅 Windsurf
#   ./install.sh cline        # 仅 Cline
#   ./install.sh copilot      # 仅 GitHub Copilot
#   ./install.sh aider        # 仅 Aider
#
# 远程一键安装:
#   curl -sSL https://raw.githubusercontent.com/zomin/chinese-osint-skills/main/install.sh | bash -s -- hermes

set -e

REPO="https://github.com/zomin/chinese-osint-skills.git"
TMPDIR=$(mktemp -d)
CLONE_DIR="$TMPDIR/chinese-osint-skills"

echo "🔍 Chinese OSINT — 跨平台安装"
echo "================================"

git clone --depth 1 "$REPO" "$CLONE_DIR" 2>/dev/null || true

install_for() {
    local platform="$1"
    case "$platform" in
        hermes)
            TARGET="$HOME/.hermes/skills/research/chinese-osint"
            mkdir -p "$TARGET"
            cp "$CLONE_DIR/SKILL.md" "$TARGET/SKILL.md"
            cp -r "$CLONE_DIR/scripts" "$TARGET/" 2>/dev/null || true
            cp -r "$CLONE_DIR/docs" "$TARGET/" 2>/dev/null || true
            echo "✅ Hermes Agent: $TARGET/SKILL.md"
            ;;
        claude)
            mkdir -p "$HOME/.claude/rules"
            cp "$CLONE_DIR/AGENTS.md" "$HOME/.claude/rules/chinese-osint.md"
            echo "✅ Claude Code (全局): ~/.claude/rules/chinese-osint.md"
            ;;
        opencode)
            mkdir -p "$HOME/.config/opencode"
            cp "$CLONE_DIR/AGENTS.md" "$HOME/.config/opencode/AGENTS.md"
            echo "✅ OpenCode (全局): ~/.config/opencode/AGENTS.md"
            ;;
        cursor)
            mkdir -p .cursor/rules
            cp "$CLONE_DIR/AGENTS.md" .cursor/rules/chinese-osint.mdc
            echo "✅ Cursor (项目级): .cursor/rules/chinese-osint.mdc"
            ;;
        windsurf)
            mkdir -p .windsurf/rules
            cp "$CLONE_DIR/AGENTS.md" .windsurf/rules/chinese-osint.md
            echo "✅ Windsurf (项目级): .windsurf/rules/chinese-osint.md"
            ;;
        cline)
            mkdir -p .clinerules
            cp "$CLONE_DIR/AGENTS.md" .clinerules/chinese-osint.md"
            echo "✅ Cline (项目级): .clinerules/chinese-osint.md"
            ;;
        copilot)
            mkdir -p .github
            cp "$CLONE_DIR/.github/copilot-instructions.md" .github/copilot-instructions.md
            echo "✅ Copilot (项目级): .github/copilot-instructions.md"
            ;;
        aider)
            cp "$CLONE_DIR/AGENTS.md" ./CONVENTIONS.md
            echo "✅ Aider: ./CONVENTIONS.md"
            echo "   请在 .aider.conf.yml 中添加: read: CONVENTIONS.md"
            ;;
        *)
            echo "❌ 未知平台: $platform"
            return 1
            ;;
    esac
}

if [ $# -eq 0 ]; then
    echo ""
    echo "用法: $0 <platform> [platform ...]"
    echo ""
    echo "支持的平台:"
    echo "  hermes     Hermes Agent   (~/.hermes/skills/)"
    echo "  claude     Claude Code    (~/.claude/rules/)"
    echo "  opencode   OpenCode       (~/.config/opencode/)"
    echo "  cursor     Cursor         (.cursor/rules/)"
    echo "  windsurf   Windsurf       (.windsurf/rules/)"
    echo "  cline      Cline          (.clinerules/)"
    echo "  copilot    GitHub Copilot (.github/)"
    echo "  aider      Aider          (./CONVENTIONS.md)"
    echo "  all        全部安装"
    echo ""
    rm -rf "$TMPDIR"
    exit 0
fi

PLATFORMS="$@"
if [ "$1" = "all" ]; then
    PLATFORMS="hermes claude opencode cursor windsurf cline copilot aider"
fi

for p in $PLATFORMS; do
    install_for "$p" || true
done

rm -rf "$TMPDIR"
echo ""
echo "🎉 安装完成！文档: https://github.com/zomin/chinese-osint-skills"
