/**
 * A problem's own text, without the sub-parts listed underneath it.
 *
 * The segmenter is told to copy statements verbatim, and it obeys: a problem with five
 * lettered parts comes back with all five inside the problem's statement *and* again as
 * structured parts. That was survivable while both were printed raw, because they at
 * least matched. Now the parts are typeset and the statement they came from is not, so
 * the same problem appears twice in two different notations, which reads as a rendering
 * fault rather than as repetition.
 *
 * Cutting at the first sub-part label is deterministic rather than clever: the labels are
 * the ones the segmenter itself assigned, so if one appears at the start of a line in the
 * statement, that line is where the sheet's list began.
 *
 * Returns the statement unchanged when there is nothing to cut, which covers a problem
 * with no sub-parts, unlabelled parts, and a segmenter that did not repeat them.
 */
export function statementLeadIn(statement: string, labels: readonly (string | null)[]): string {
  const first = labels.find((label) => label !== null && label.trim() !== '')?.trim()
  if (first === undefined) return statement

  const lines = statement.split('\n')
  const index = lines.findIndex((line) => line.trim().startsWith(first))
  // Nothing before it is not a lead-in, it is the whole statement.
  if (index <= 0) return statement

  const lead = lines.slice(0, index).join('\n').trim()
  return lead === '' ? statement : lead
}
