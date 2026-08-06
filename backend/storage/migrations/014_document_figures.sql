-- Figures found in a document, which are the first pipeline output that is not text.
--
-- Stored rather than found on demand for the same reason chunks are: locating them means
-- opening the file and walking every page, and the solver asks per problem while a student
-- watches. The row is small and the file it describes does not change.
--
-- `bbox` is JSON `[x0, y0, x1, y1]` as fractions of the page box, the convention
-- `artifact_provenance.bbox` and `rag/locate.py` already use, because a page renders as an
-- image at whatever width the pane happens to have.
--
-- `label` is null for most figures and that is not a defect. Five of the sixty-nine figures
-- in the reference course carry a caption at all, so a figure is normally identified by the
-- page and position it was found at. Nothing here guesses an owner for an uncaptioned
-- figure: on the acceptance document the numbered list markers sit *below* their diagrams,
-- so the obvious heuristic attaches every figure to the problem before its own.
create table document_figures (
  id integer primary key autoincrement,
  document_id integer not null references documents(id) on delete cascade,
  page_number integer not null,
  figure_index integer not null,
  bbox text not null,
  label text,
  caption text
);
create index idx_figures_document on document_figures(document_id, page_number);
create unique index idx_figures_position on document_figures(document_id, page_number, figure_index);
