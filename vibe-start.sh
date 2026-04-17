#!/bin/zsh

BRANCH=$1
BASE=${2:-main}

if [ -z "$BRANCH" ]; then
    echo "Usage: ./vibe-start $BRANCH_NAME [$BASE_BRANCH]"
    exit 1
fi

REPO_ROOT=$(git rev-parse --show-toplevel)
REPO_NAME=$(basename "$REPO_ROOT")
TARGET_DIR="../$REPO_NAME-worktrees/$BRANCH"
SESSION_NAME="vibe-$BRANCH"

# --- 1. WORKTREE SETUP ---
if [ -d "$TARGET_DIR" ]; then
    echo "🔄 Worktree exists. Resuming..."
else
    echo "🚀 Creating NEW worktree for '$BRANCH'..."
    git worktree add -b "$BRANCH" "$TARGET_DIR" "$BASE"
    cp "$REPO_ROOT/.env" "$TARGET_DIR/" 2>/dev/null
    cd "$TARGET_DIR"
    # Adjust this install command to your stack
    npm install &>/dev/null 
fi

cd "$TARGET_DIR"

# --- 2. TMUX & AGENT ORCHESTRATION ---
tmux has-session -t "$SESSION_NAME" 2>/dev/null

if [ $? != 0 ]; then
    # Create session
    tmux new-session -d -s "$SESSION_NAME" -n "coding"
    tmux split-window -h
    
    # --- PANE 0: LEAD ENGINEER (CODEX) ---
    # Define the "Amnesia Fix" prompt for Codex
    CODEX_PROMPT="You are the Lead Engineer in a Git Worktree for branch: $BRANCH. Your partner is Claude (in the right pane), who is the Planner. Focus strictly on writing high-quality code. Do not worry about Git commits; Claude handles that. Do nothing for now, await further instructions."

    tmux select-pane -t 0
    tmux send-keys "codex" Enter
    sleep 2 # Give the CLI a second to boot before typing the prompt
    tmux send-keys "$CODEX_PROMPT" Enter

    # --- PANE 1: PLANNER & REVIEWER (CLAUDE) ---
    # Define the "Amnesia Fix" prompt for Claude
    CLAUDE_PROMPT="You are the Lead Planner and Reviewer in a Git Worktree for branch: $BRANCH. Your partner is Codex (in the left pane), who is the Engineer. You are responsible for reviewing Codex's file changes, running tests, and performing all 'git add' and 'git commit' operations. Do nothing for now, await further instructions."

    tmux select-pane -t 1
    tmux send-keys "claude" Enter
    sleep 2
    tmux send-keys "$CLAUDE_PROMPT" Enter
fi

# Open VS Code for the worktree
code .

# Attach to the session
tmux attach-session -t "$SESSION_NAME"