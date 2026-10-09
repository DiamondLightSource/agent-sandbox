import { test, expect } from 'claude-code/testing'
import { isQuiet } from './quiet-state'

test('goal folder, pointer and status writes are quiet', () => {
  expect(isQuiet('Edit', { file_path: '/root/.claude/orchestrate/fix-x/state.md' })).toBe(true)
  expect(isQuiet('Write', { file_path: '/root/.claude/orchestrate/fix-x/briefs/a.md' })).toBe(true)
  expect(isQuiet('Read', { file_path: '/home/u/.claude/orchestrate/fix-x/maps/repo.md' })).toBe(true)
  expect(isQuiet('Write', { file_path: '/workspaces/proj/.claude/status.md' })).toBe(true)
  expect(isQuiet('Bash', { command: 'cat .claude/status.md' })).toBe(true)
  expect(isQuiet('Bash', { command: 'cat ~/.claude/orchestrate/active' })).toBe(true)
  expect(isQuiet('Bash', { command: 'ls ~/.claude/orchestrate' })).toBe(true)
  expect(isQuiet('Bash', { command: 'echo '- entry' >> ~/.claude/orchestrate/fix-x/log.md' })).toBe(true)
})

test('everything else is drawn as usual', () => {
  expect(isQuiet('Edit', { file_path: '/workspaces/proj/src/main.py' })).toBe(false)
  expect(isQuiet('Write', { file_path: '/root/.claude/projects/-workspaces-proj/memory/MEMORY.md' })).toBe(false)
  expect(isQuiet('Write', { file_path: '/root/.claude/state/-workspaces-proj/goal.md' })).toBe(false)
  expect(isQuiet('Write', { file_path: '/root/.claude/orchestrate-notes/x.md' })).toBe(false)
  expect(isQuiet('Write', { file_path: '/workspaces/proj/.claude/notes/status.md' })).toBe(false)
  expect(isQuiet('Write', { file_path: '/workspaces/proj/.claude/status.md.bak' })).toBe(false)
  expect(isQuiet('Edit', { file_path: '/root/.claude/settings.json' })).toBe(false)
  expect(isQuiet('Bash', { command: 'pytest tests' })).toBe(false)
  expect(isQuiet('Bash', { command: 'cat x.claude/status.md' })).toBe(false)
  expect(isQuiet('Agent', { prompt: '/root/.claude/orchestrate/fix-x/' })).toBe(false)
})
