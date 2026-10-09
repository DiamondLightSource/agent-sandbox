import type { Register } from 'claude-code'

// Writes that are bookkeeping, not conversation: the goal folders and the
// pointer under ~/.claude/orchestrate/, and the status file the user reads
// in their editor (<launch dir>/.claude/status.md).
const QUIET = /\.claude\/orchestrate(\/|$|[\s'"])|(^|[\s\/'"])\.claude\/status\.md(?![\w.\/-])/

export function isQuiet(tool: string, input: unknown): boolean {
  if (typeof input !== 'object' || input === null) return false
  const i = input as Record<string, unknown>
  if (['Edit', 'Write', 'MultiEdit', 'Read'].includes(tool)) {
    return typeof i.file_path === 'string' && QUIET.test(i.file_path)
  }
  if (tool === 'Bash') {
    return typeof i.command === 'string' && QUIET.test(i.command)
  }
  return false
}

export const register: Register = on => {
  // Results carry no input, so remember which calls were quiet.
  const quietIds = new Set<string>()

  on('tool.call', ($, e, next) => {
    if (isQuiet(e.tool, e)) quietIds.add(e.tool_use_id)
    return next(e)
  })

  on('ui.render', { component: 'ToolUse' }, ($, e, next) => {
    if (e.props.isErrored || !isQuiet(e.props.tool, e.props.input)) return next(e)
    quietIds.add(e.props.tool_use_id)
    const { Text } = $.ui.resolve(e)
    return <Text dimColor>· state updated</Text>
  })

  on('ui.render', { component: 'ToolResult' }, ($, e, next) => {
    if (e.props.isErrored || !quietIds.has(e.props.tool_use_id)) return next(e)
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
}
