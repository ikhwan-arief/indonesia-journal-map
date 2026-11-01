export function bubbleRadius(count, maximum) {
  const safeCount = Math.max(0, Number(count) || 0);
  const safeMaximum = Math.max(1, Number(maximum) || 1);
  return Math.max(5, Math.min(35, 5 + 30 * Math.sqrt(safeCount / safeMaximum)));
}
