# AGENTS.md — Coding Agent Instructions

## Project Overview

**Fork of `contentauth/c2pa-python`** with patches for CAWG identity assertion support. This fork adds:

1. **`Signer.add_dynamic_assertion(callback, label, reserve_size)`** — Register a Python callback invoked during signing with the `PartialClaim` (all assertion hashed URIs). The callback returns CBOR-encoded assertion content with correct `referenced_assertions` hashes.

2. **Settings-based signer fix** — `Builder.sign()` now works when a `Context` has a signer from Settings (e.g. `cawg_x509_signer`) even without an explicit Python `Signer` object.

3. **Multiple DynamicAssertions with same label** — The c2pa-rs fix in the submodule allows two `cawg.identity` assertions (e.g. tool identity + human identity) in the same manifest.

Used by the [stardustproof-c2pa-signer-vibe](https://github.com/mstattma/stardustproof-c2pa-signer-vibe) library.

## Architecture

```
c2pa-python (this repo)
├── src/c2pa/c2pa.py          ← Python bindings (patched)
├── src/c2pa/libs/libc2pa_c.so ← Native library (built from submodule)
├── c2pa-rs/                   ← Git submodule: mstattma/c2pa-rs fork
│   ├── c2pa_c_ffi/            ← C FFI layer (patched)
│   └── sdk/                   ← Core c2pa-rs SDK (patched)
├── build_native.py            ← Build script for the native library
└── setup.py                   ← Modified to build from source
```

The native library (`libc2pa_c.so` / `.dylib` / `.dll`) is built from the `c2pa-rs` git submodule which contains our Rust patches. The submodule tracks `mstattma/c2pa-rs` branch `feat/dynamic-assertion-ffi`.

## Patches Summary

### Python (`src/c2pa/c2pa.py`)

| Change | Location | Description |
|--------|----------|-------------|
| `DynamicAssertionCallback` type | ~line 370 | ctypes callback type for the FFI |
| `Signer.add_dynamic_assertion()` | ~line 3045 | Python API: register a callback, invokes C FFI `c2pa_signer_add_dynamic_assertion` |
| `has_signer` fix | ~line 3700 | `_sign_common` tries context-based signing when `self._context is not None` |
| `Builder.sign_fragmented()` | ~line 3782 | Python API for fragmented BMFF signing; invokes C FFI `c2pa_builder_sign_fragmented` |

### Rust — C FFI (`c2pa-rs/c2pa_c_ffi/src/c_api.rs`)

| Change | Description |
|--------|-------------|
| `DynamicAssertionCallback` | C function pointer type |
| `FfiDynamicAssertion` | Implements `DynamicAssertion` trait, serializes `PartialClaim` as JSON, invokes C callback |
| `FfiDynamicSignerV2` | Wraps inner `Signer`, delegates all methods, overrides `dynamic_assertions()` |
| `c2pa_signer_add_dynamic_assertion()` | Exported FFI function |
| `c2pa_builder_sign_fragmented()` | Exported FFI function — wraps `Builder::sign_fragmented_files`, reads back manifest bytes from the signed init segment via `jumbf_io::load_jumbf_from_file` and returns them through `manifest_bytes_ptr` |

### Rust — SDK (`c2pa-rs/sdk/src/`)

| Change | File | Description |
|--------|------|-------------|
| `__N` suffix stripping in referenced_assertions matching | `identity/builder/identity_assertion_builder.rs` | `cawg_x509_signer` referenced_assertions `"c2pa.soft-binding"` now matches `c2pa.soft-binding__1` etc. |
| `replace_assertion_by_instance()` | `claim.rs` | New method: replaces assertion at a specific instance index |
| Label dedup in `write_dynamic_assertions` | `store.rs` | Resolves `__N`-suffixed labels from claim URIs so multiple DAs with same label target correct placeholders |
| `.m4s` + `.cmfv` added to BMFF `SUPPORTED_TYPES` | `asset_handlers/bmff_io.rs` | Required for DASH/HLS segmented asset sets (`sign_fragmented`) to pass `get_supported_file_extension` inside `save_to_bmff_fragmented` |

## Build & Install

### Prerequisites

- **Python 3.10+**
- **Rust toolchain** (for building the native library from source)

```bash
# Install Rust (one-time, all platforms)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

### Setup

```bash
# Clone with submodule
git clone --recurse-submodules https://github.com/mstattma/c2pa-python.git
cd c2pa-python
git checkout feat/dynamic-assertion-callback

# Build the native library (~3-5 min first time, ~30s incremental)
python build_native.py

# Install
pip install -e .
```

### Rebuild after changes

If you modify the Rust code in `c2pa-rs/`:

```bash
python build_native.py    # rebuilds and copies .so/.dylib/.dll
pip install -e .          # re-installs with new library
```

If you only modify `src/c2pa/c2pa.py`, a re-install is sufficient (editable install picks up changes automatically for `-e .`).

## Rules

- **Do not modify upstream c2pa-python files** unless necessary for the patches. Keep changes minimal and well-commented for future PR submission.
- The `c2pa-rs` submodule is pinned to a specific commit on `feat/dynamic-assertion-ffi`. Update with `cd c2pa-rs && git pull origin feat/dynamic-assertion-ffi` then rebuild.
- The `src/c2pa/libs/` directory is gitignored — the native library is built locally per platform.
- The `DynamicAssertionCallback` GC prevention pattern (storing ctypes callback refs in `self._dynamic_assertion_cbs`) follows the same pattern as `Signer.from_callback()`.
- `build_native.py` tries `cargo` on PATH, then `~/.cargo/bin/cargo`. It builds with `--features file_io`.

## Testing

```bash
# Run upstream tests (should still pass)
python -m pytest tests/ -v

# Quick smoke test for DynamicAssertion
python -c "
import c2pa
s = c2pa.Signer.from_info(c2pa.C2paSignerInfo(alg=b'es256', sign_cert=b'...', private_key=b'...'))
print('add_dynamic_assertion' in dir(s))  # True
"
```

## When You Change These, Update Docs Too

| What changed | Update |
|---|---|
| DynamicAssertion callback signature | `c2pa.py` Python API, `c_api.rs` FFI, `AGENTS.md` |
| c2pa-rs submodule commit | `git submodule update`, rebuild, `AGENTS.md` |
| Build process | `build_native.py`, `setup.py`, `AGENTS.md` |
| Platform support | `setup.py` platform detection, `build_native.py` lib names |
