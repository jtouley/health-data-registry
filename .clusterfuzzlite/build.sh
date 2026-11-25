#!/bin/bash -eu
# Builds every Atheris harness in fuzz/ into $OUT, with its seed corpus, dictionary and libFuzzer options.
#
# The OSS-Fuzz Python image runs Python 3.11 while the package needs 3.12, so the package isn't pip-installed:
# PyInstaller bundles src/ directly (--paths src). The modules the harnesses import run on 3.11 (query.py, which
# doesn't, is never imported by them). Dependencies come hash-pinned from fuzz/requirements.txt, so the build
# installs exactly the reviewed wheels and nothing else (--no-deps).

pip3 install --require-hashes --no-deps -r fuzz/requirements.txt

for fuzzer in fuzz/fuzz_*.py; do
  name=$(basename -s .py "$fuzzer")
  # sqlglot loads its dialects by name at run time, which PyInstaller can't see.
  compile_python_fuzzer "$fuzzer" --paths src --collect-submodules sqlglot

  if [ -d "fuzz/corpus/$name" ]; then
    (cd "fuzz/corpus/$name" && zip -q -r "$OUT/${name}_seed_corpus.zip" .)
  fi
  if [ -f "fuzz/$name.dict" ]; then
    cp "fuzz/$name.dict" "$OUT/$name.dict"
  fi
  cp "fuzz/$name.options" "$OUT/$name.options"
done
