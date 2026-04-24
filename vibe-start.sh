#!/bin/zsh

# --- DEFAULTS ---
BASE_BRANCH="DEV"  # Changed from main to DEV

# --- FLAG PARSING ---
# Usage: ./vibe-start.sh [-r reference_branch] <new_branch_name>
while getopts "r:" opt; do
  case $opt in
    r) BASE_BRANCH=$OPTARG ;;
    \?) echo "Invalid option: -$OPTARG" >&2; exit 1 ;;
  esac
done

# Shift the arguments so $1 becomes the new branch name
shift $((OPTIND -1))
NEW_BRANCH=$1

if [ -z "$NEW_BRANCH" ]; then
    echo "Usage: ./vibe-start [-r base_branch] <new_branch_name>"
    echo "Default base branch is: $BASE_BRANCH"
    exit 1
fi

REPO_ROOT=$(git rev-parse --show-toplevel)
REPO_NAME=$(basename "$REPO_ROOT")
TARGET_DIR="../$REPO_NAME-worktrees/$NEW_BRANCH"
SESSION_NAME="vibe-$NEW_BRANCH"

# --- 1. WORKTREE SETUP ---
if [ -d "$TARGET_DIR" ]; then
    echo "🔄 Worktree exists for '$NEW_BRANCH'. Resuming..."
else
    echo "🚀 Creating NEW worktree: $NEW_BRANCH (Based on: $BASE_BRANCH)"
    # git worktree add -b <new-branch> <path> <start-point>
    git worktree add -b "$NEW_BRANCH" "$TARGET_DIR" "$BASE_BRANCH"
    
    # Environment Setup
    cp "$REPO_ROOT/.env" "$TARGET_DIR/" 2>/dev/null
    cd "$TARGET_DIR"
    npm install &>/dev/null 
fi

cd "$TARGET_DIR"

# --- 2. TMUX & AGENT ORCHESTRATION ---
tmux has-session -t "$SESSION_NAME" 2>/dev/null

if [ $? != 0 ]; then
    tmux new-session -d -s "$SESSION_NAME" -n "coding"
    tmux split-window -h
    
    # PANE 0: LEAD ENGINEER (CODEX)
    CODEX_PROMPT="You are the Lead Engineer in a Git Worktree for branch: $NEW_BRANCH (base: $BASE_BRANCH). Your partner is Claude (in the right pane). Focus strictly on writing code. Claude handles Git. Do nothing for now, await further instructions."
    tmux select-pane -t 0
    tmux send-keys "codex" Enter
    sleep 5
    tmux send-keys "$CODEX_PROMPT" Enter

    # PANE 1: PLANNER & REVIEWER (CLAUDE)
    CLAUDE_PROMPT="You are the Lead Planner/Reviewer in a Git Worktree for branch: $NEW_BRANCH (base: $BASE_BRANCH). Codex (left pane) is the Engineer. You review changes and handle all 'git add/commit' operations. Do nothing for now, await further instructions."
    tmux select-pane -t 1
    tmux send-keys "claude" Enter
    sleep 5
    tmux send-keys "$CLAUDE_PROMPT" Enter
fi

code .
tmux attach-session -t "$SESSION_NAME"