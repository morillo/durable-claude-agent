# vhs tape: records `make demo-crash` to docs/demo/crash-recovery.gif
# Prereqs in other terminals: make temporal, make mcp. No `make worker` running.
# Re-record with `make record-demo` (brew install vhs).
Output docs/demo/crash-recovery.gif

Set Shell zsh
Set FontSize 15
Set Width 1200
Set Height 820
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Padding 16

Type "make demo-crash"
Sleep 800ms
Enter
# The demo takes ~70-90s: two worker start-ups, three Claude turns, a 10s heartbeat timeout.
Sleep 105s
