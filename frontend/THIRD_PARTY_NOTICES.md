# Third-party notices (web app)

## Simple Icons (technology logos on canvas nodes)

Logo paths come from the `simple-icons` package (https://github.com/simple-icons/simple-icons), whose collection is
released under CC0 1.0. CC0 for the collection does not mean every underlying mark is free of conditions; the
marks remain trademarks of their owners and are shown only to identify the technology a component uses. Their
use does not imply endorsement. The curated list is in `src/brandIcons.ts`; marks whose recorded licence adds
conditions (non-commercial, attribution or custom terms) are deliberately left out. Apache Kafka, Apache Airflow,
Apache Spark and Apache Cassandra marks are recorded as Apache-2.0 and remain subject to the ASF trademark policy.

## Libraries

- `html-to-image` (MIT): PNG and SVG export of the canvas.
- `lucide-react` (ISC): the interface icons (`src/icons.tsx`, component types in `src/components/ArchNode.tsx`).
- `@fontsource-variable/geist` and `@fontsource-variable/geist-mono` (SIL Open Font License 1.1): the Geist and Geist Mono
  typefaces, bundled with the app so no font is fetched from a third-party server.
- `mermaid` (MIT): drawing the sequence and data-model views of a component's low-level design.

Interactive HTML exports are rendered on the server by Archify (MIT); see `backend/vendor/archify/VENDORED.md`
and `backend/vendor/archify/THIRD_PARTY_NOTICES.md`.
