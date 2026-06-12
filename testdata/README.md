# testdata

Folder for real ROMs used by the **optional** tests.

This project does not include commercial ROMs for copyright reasons. The
`OptionalRealRomTest` in `tests/test_core.py` only runs if it finds the matching
file here; otherwise it is skipped automatically.

To enable it, place your own legitimate copy with this exact name:

```
testdata/Kirby's Dream Land 3 (USA).sfc
```

All other tests use synthetic ROMs generated in memory and need no external file.
