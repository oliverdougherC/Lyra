-- Drop the folder path from documents uploaded before the upload route stopped keeping it.
--
-- A folder upload sends each file's path relative to the chosen folder as its multipart
-- filename, and the whole of it was stored: a term of notes filled the document list with
-- one folder name repeated, every row truncated in the middle to the same few characters.
-- `stored_path` was never affected -- it has always been built from the basename -- so this
-- touches only what the student reads.
--
-- SQLite has no `reverse`, so the folder part is found the way it can be: `replace` gives
-- the name's characters minus the separator, and `rtrim` against that set eats everything
-- after the final separator, leaving the prefix whose length is where the basename starts.
-- The second condition is the guard for a name that is nothing but separators, which would
-- otherwise be renamed to the empty string.
update documents
set filename = substr(filename, length(rtrim(filename, replace(filename, '/', ''))) + 1)
where filename like '%/%'
  and substr(filename, length(rtrim(filename, replace(filename, '/', ''))) + 1) <> '';
