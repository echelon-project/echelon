# Gates — how a claim becomes a fact

A gate is the thing standing between "I believe this works" and "this works."

## The first law: prove, don't claim

Nothing is fixed, done, or working until you re-ran the exact thing that failed and
watched it pass. The sentence *"this should work now"* is banned. The sentence
*"I ran it, here is the output"* replaces it.

This sounds obvious and is violated constantly, because the forward-motion bias is
strong and re-running is boring. It is also where most wasted hours come from: not
from bugs, but from believing a bug was fixed and building on the belief.

## The gates that pay for themselves

**A gate that is not committed did not happen.** A check living only in your shell
history protects nothing tomorrow.

**Green suite is not a working product.** Tests exercise what you thought to test.
Before you call a surface done, use it the way a user will.

**Grep is not evidence.** Finding the string `handleSubmit` proves the string
exists. It does not prove the handler is wired, reachable, or called. Run the path.

**A test that never calls the handler cannot see a missing call.** Assert against
the real entry point, not a helper you invoke directly in the test.

**Break it yourself.** After a fix, deliberately re-introduce the failure and
confirm your new test goes red. A test that passes both before and after your change
is testing nothing. This one catches more no-op fixes than any other habit.

**An exit code after a pipe is the pipe's exit code.** `make build | tail -5; echo $?`
reports `tail`'s status. Gate on the command, never on the pipeline's tail.

## Isolate before you blame

When a pipeline breaks, reproduce each layer alone: the provider call by itself, the
tool by itself, then the combination. The layer that still fails alone is the
suspect. Never change two things before re-testing one.

And when you are stuck after two attempts, stop reasoning and go read the wire —
logs, dumps, raw requests, actual bytes. The bug confesses in evidence, never in your
head. A theory that pattern-matches a familiar failure is a hypothesis, not a
verdict; "it's probably latency" is plausible every time and usually wrong.

## Who is allowed to gate

**Nothing grades its own homework.** A generator validating its own output shares
the blind spot that produced the flaw. If a model wrote the code, the clearing has to
come from a context that did not write it — a different pass, a different model, a
test authored before the fix, or a human.

This is the same principle as earn-by-trace at the level of a work unit: the claim
does not become a fact because its author is confident. It becomes a fact when
something that could have said no, didn't.

See also: [DISCIPLINE.md](DISCIPLINE.md), [BOUNDARY-DRIVEN.md](BOUNDARY-DRIVEN.md).
