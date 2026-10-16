# SQLite Vec1 source pin

`vec1.c`, `sqlite3.h`, and `sqlite3ext.h` are copied without modification from
SQLite 3.54.0 at check-in
`be8d059e9a49089ab2dce5ed26dd87aaf598fdc5fbe0b107c0dd758464bcd5e3`.

- Upstream: <https://sqlite.org/src/file/ext/vec1/vec1.c>
- Vec1 version: 0.8
- `vec1.c`: 371445 bytes, SHA-256
  `4f9a83efd78992769ba7a8e630d755b93370258387694498f22d1eb3cfd3e766`
- `sqlite3.h`: 695737 bytes, SHA-256
  `cd20a2877bc82b94e36c1093b727dd87cfa3c1d581aac6e240925535cbd90166`
- `sqlite3ext.h`: 39547 bytes, SHA-256
  `48963abfefb41ac329ef9d706acb5eadba952a99a748285c9ded6b5e0942721b`
- License: public domain dedication in the source header

The repository build script verifies all three hashes before invoking a native
compiler. Frisket ships the resulting loadable SQLite extension in its
platform package; it does not compile or download Vec1 at runtime.
