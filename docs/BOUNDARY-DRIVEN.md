# Boundary-driven development

The default work map: before writing code, name the **boundary** the work lives
inside. Borrowed from how large desktop applications are actually structured, and
applied to any language.

## The four layers

**DOOR** — the executable, the web UI, the CLI. It has *no logic*. It reads input,
calls the host, renders output. If you can delete the door and the system still
works headlessly, the door is thin enough.

**HOST** — reads the wiring, resolves which packages to load, routes an action to
the package that owns it. The host knows *what exists*; it does not know *how
anything works*.

**WIRING** — the manifest, the settings, the config, the environment. **Files, never
code.** A hostname, a roster of projects, a feature toggle, an allowlist of emails:
each is data a deployment supplies, not a constant a developer bakes in. This is the
layer people skip, and skipping it is why software runs on one machine.

**PACKAGES** — assembly-like units. One task each, a declared interface, a version,
and its own scenario suite. A package is done when its scenarios pass against its
interface — not when the author feels finished.

## The rules that give it teeth

**Name the package before you build.** "Where does this go?" answered after the code
exists is answered by whichever file you happened to open.

**The scenario suite is the DONE gate.** Not "it works on my branch." The package
declares what it promises, and the suite proves the promise against the interface a
caller actually sees.

**Extract wiring from code.** Every hardcoded host, path, roster, or credential is a
boundary violation. Default to empty or to a documented environment variable, and
let the deployment fill it in. A library that cannot run on a machine other than its
author's has no boundary — it has a habit.

**A unit that touches more than one package is a design smell**, or it is genuinely
a cross-cutting change that deserves to be planned as one.

## Freeze is a boundary, not a compilation

To "freeze" a package is to fix its **version + interface + contract test**, and tag
it. It is not to compile it into an opaque binary. A frozen package can still be
read, debugged and patched; what it cannot do is change its promises silently.

Compilation, if it ever happens, belongs at the release door — never as a way of
declaring something stable.

## Why this belongs in a memory substrate's docs

Because the same discipline governs the bank. An atom has a boundary (one lesson), a
declared interface (its description), and a contract test (does reading it change
behavior?). A scope is a package boundary for memory. The estate is the host. The
CLI is the door. It is one idea applied at two altitudes.

See also: [ARCHITECTURE.md](ARCHITECTURE.md), [GATES.md](GATES.md).
