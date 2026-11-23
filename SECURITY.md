# Security and safety policy

OpenCutter drives a machine with a sharp blade. A bug that makes the machine move when it should
not is as serious to us as a security hole, so both are reported the same way: privately, not in
a public issue.

**If a machine is moving now and should not be, press its power button.** That stop needs no
computer. OpenCutter's own stop needs the Bluetooth link, and what it does is only partly known
(see [Safety](https://github.com/MortyNiners/opencutter#safety) in the README).

## What to report privately

Safety problems:

- **The machine moves when it should not**: before you pressed Send or Cut, after the job ended
  or was cancelled, while it was paused and nobody pressed its Pause key or Resume, or with a
  tool down where it should be up.
- **A stop that does not stop**: the machine keeps moving after Cancel Job, Stop Machine or
  `opencutter stop`, or the app says a stop went out when it did not.
- **A command sent that should not be**: a command from the never-send list
  ([docs/PROTOCOL.md](https://github.com/MortyNiners/opencutter/blob/main/docs/PROTOCOL.md),
  section 6), a Pause or Resume that nobody asked for, a value outside the model's ranges, a job
  for another model than the one connected, or a job that the app's checks should have refused.

Security problems:

- another user or program on the computer can make OpenCutter's background service move the
  machine, or read or change its jobs;
- a design or presets file made to attack OpenCutter makes it run code, read or write files it
  should not, or send the machine something it should not;
- the Flatpak has access it does not need, or uses it for something it should not do.

If you are not sure whether something is a safety problem, report it privately anyway. Other bugs
go to the [issue tracker](https://github.com/MortyNiners/opencutter/issues).

## How to report

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and press
**Report a vulnerability**, or go straight to the
[private report form](https://github.com/MortyNiners/opencutter/security/advisories/new). Only
you and the maintainers can see the report. Please do not open a
public issue, discussion or pull request about it until a fix is released.

## What to include

- OpenCutter's version and build: the output of `opencutter --version` (in the Flatpak,
  `flatpak run --command=opencutter io.github.mortyniners.OpenCutter --version`), and whether it
  is the Flatpak bundle or a copy from source.
- The machine and its firmware, as the **Firmware** row of the connection menu or the `model:`
  line of `opencutter status` shows them.
- What happened, step by step, and what you expected. For a machine that moved: what it did and
  when, and whether you needed the power button.
- The lines of the service log, `daemon.log`, from the connection to the end, and of `app.log`
  if the app showed an error. Both files are in
  `~/.var/app/io.github.mortyniners.OpenCutter/.local/state/opencutter/` in the Flatpak, and in
  `~/.local/state/opencutter/` from source (`$XDG_STATE_HOME/opencutter/` when that is set).
- For a problem that a file triggers: the file, or a smaller one that still triggers it, if you
  made it yourself and may share it.

Replace the machine's Bluetooth name (it holds the serial number) and its Bluetooth address in
everything you send, and leave out your user name and home folder paths. Please send no files
made with Silhouette software and no firmware files.

## What happens next

OpenCutter is a small volunteer project, so there is no fixed response time. We will confirm that
the report arrived, look into it as soon as we can and tell you what we found. A fix comes out as
a new release whose notes say what it fixes, and we gladly credit you there if you wish.

## Supported versions

OpenCutter is alpha software (version 0.x). Fixes go into the newest release only, so please
check that the problem still happens there before you report it, if you can.
