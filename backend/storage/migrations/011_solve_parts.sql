-- Whether a problem's lettered parts are one solution or several.
--
-- A sheet that reads "For each system below, determine whether it is linear: (a) ... (e)"
-- is five questions with five answers, and solving it as one problem produced one run of
-- working, one answer sentence holding five results, and one verdict covering all of
-- them: a refutation of (c) marked (a), (b), (d) and (e) wrong with it, and re-solving
-- (c) meant re-solving the other four. Retrieval was worse still -- the query was the
-- shared instruction, which names no mathematics at all.
--
-- A sheet that reads "(a) Find $X(j\omega)$. (b) Using your answer to (a), ..." is one
-- solution and has to stay one, because (b) cannot be solved without (a).
--
-- The two are told apart per problem, at segmentation, and the student can correct the
-- reading at the review gate like every other part of it. `together` is the default and
-- the old behaviour: nothing that exists changes shape until something says it should.
alter table artifact_parts add column solve_parts text not null default 'together'
  check (solve_parts in ('together','separately'));
