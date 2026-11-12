// media-library's headless Joplin client: the To watch / To read lists (D73). It runs next to the Joplin CLI in the
// joplin container (client.sh); the CLI does every sync and every write, this only sets it up, reads its local
// database and checks the sync target. Nothing here ever writes to the sync target itself.
//
//   node lists.mjs setup          the sync settings from the environment and the password file (idempotent)
//   node lists.mjs guard before   refuse to sync when it would change what the owner's devices share (see below);
//                                 remember the target's shared settings
//   node lists.mjs guard after    the target's shared settings must be as before the sync, else stop for good
//   node lists.mjs guard show     print them
//   node lists.mjs notes          the list notes from the local database, as JSON on stdout
//   node lists.mjs strip          stdin {"notes": [{"id", "remove": [lines]}]}: delete those lines (each once) from
//                                 each note as it is now, unless it was edited in the last JOPLIN_LISTS_QUIET_MINUTES
//                                 (20); each note is read back from the database; a note the CLI did not store as
//                                 sent is put back, and if that fails too nothing syncs again until a person looks
//
// What the owner's devices share is the target's info.json: the sync format (3), end-to-end encryption, the note
// history settings and appMinVersion. A client raises appMinVersion to its own when it syncs, and older apps then
// refuse to sync: the guard refuses before that. This client changes no shared setting (setup sets only the target,
// its login and that attachments are fetched on demand), and the guard checks after every sync that nothing changed.
import {DatabaseSync} from "node:sqlite";
import {execFileSync} from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {createRequire} from "node:module";

const env = process.env;
const DATA = env.JOPLIN_DATA || "/data";
const PROFILE = env.JOPLIN_PROFILE || path.join(DATA, "profile");
const STOP = path.join(DATA, "STOPPED");
const SNAP = path.join(DATA, "guard-before.json");
const JOPLIN = env.JOPLIN_BIN || "joplin";
const TARGET = String(env.JOPLIN_SYNC_TARGET || "5");          // 5 Nextcloud, 6 WebDAV; 2 a folder (tests)
const TITLES = (env.JOPLIN_LISTS_TITLES || "To watch|To read").split("|").map(t => t.trim().toLowerCase()).filter(Boolean);
const NOTEBOOK = (env.JOPLIN_LISTS_NOTEBOOK || "").trim().toLowerCase();
// A note edited this recently is left alone for now: the owner may be at it on a device that has not synced yet, and
// two changes to one note make a Joplin conflict copy. Its lines go on a later run (their items are in already).
const QUIET_MS = (+env.JOPLIN_LISTS_QUIET_MINUTES || 20) * 60e3;

const die = (code, msg) => { process.stderr.write("joplin-lists: " + msg + "\n"); process.exit(code); };
const stop = msg => { fs.writeFileSync(STOP, new Date().toISOString() + " " + msg + "\n"); die(4, msg + " (stopped: delete " + STOP + " once it is understood)"); };
const joplin = (args, input) => execFileSync(JOPLIN, ["--profile", PROFILE, ...args],
  {input, encoding: "utf8", stdio: [input == null ? "ignore" : "pipe", "pipe", "pipe"], timeout: 15 * 60e3, maxBuffer: 64 << 20});
const db = () => new DatabaseSync(path.join(PROFILE, "database.sqlite"), {readOnly: true});

function password() {
  const f = env.JOPLIN_WEBDAV_PASSWORD_FILE || "/run/secrets/joplin-webdav.pw";
  try { return fs.readFileSync(f, "utf8").replace(/\r?\n$/, ""); } catch (e) { die(2, "no password file " + f); }
}

function setup() {
  const s = {"sync.target": +TARGET, "sync.resourceDownloadMode": "manual"};
  if (TARGET === "2") s["sync.2.path"] = env.JOPLIN_SYNC_PATH;
  else {
    if (!env.JOPLIN_WEBDAV_URL || !env.JOPLIN_WEBDAV_USER) die(2, "set JOPLIN_WEBDAV_URL and JOPLIN_WEBDAV_USER");
    Object.assign(s, {[`sync.${TARGET}.path`]: env.JOPLIN_WEBDAV_URL, [`sync.${TARGET}.username`]: env.JOPLIN_WEBDAV_USER,
      [`sync.${TARGET}.password`]: password()});
  }
  joplin(["config", "--import"], JSON.stringify(s));            // on stdin: the password never is an argument
}

// --------------------------------------------------------------------- the guard
const vnum = v => String(v || "0").split(".").map(x => parseInt(x, 10) || 0);
const vcmp = (a, b) => { const x = vnum(a), y = vnum(b); for (let k = 0; k < 3; k++) if ((x[k] || 0) !== (y[k] || 0)) return (x[k] || 0) - (y[k] || 0); return 0; };
function ownMinVersion() {                                     // the appMinVersion this CLI writes to the target
  const req = createRequire(execFileSync("sh", ["-c", "readlink -f \"$(command -v " + JOPLIN + ")\""], {encoding: "utf8"}).trim());
  const f = req.resolve("@joplin/lib/services/synchronizer/syncInfoUtils.js");
  const m = fs.readFileSync(f, "utf8").match(/let appMinVersion_ = '([0-9.]+)'/);
  if (!m) die(2, "can't tell which appMinVersion this Joplin CLI writes (" + f + ")");
  return m[1];
}
async function remoteInfo() {
  let text;
  if (TARGET === "2") {
    try { text = fs.readFileSync(path.join(env.JOPLIN_SYNC_PATH, "info.json"), "utf8"); } catch (e) { text = null; }
  } else {
    const url = env.JOPLIN_WEBDAV_URL.replace(/\/+$/, "") + "/info.json";
    let r;
    try { r = await fetch(url, {headers: {Authorization: "Basic " + Buffer.from(env.JOPLIN_WEBDAV_USER + ":" + password()).toString("base64")}}); }
    catch (e) { die(3, "can't reach the sync target (" + (e.cause?.code || e.message) + "): nothing done"); }
    if (r.status === 404) text = null;
    else if (!r.ok) die(3, "the sync target answered " + r.status + " for info.json (login? address?)");
    else text = await r.text();
  }
  if (text == null) die(3, "no info.json on the sync target: it is not one your Joplin apps sync to (this client never starts one)");
  return JSON.parse(text);
}
const shared = i => ({version: i.version, e2ee: !!i.e2ee?.value, masterKeys: (i.masterKeys || []).length,
  activeMasterKeyId: i.activeMasterKeyId?.value || "", ppk: !!i.ppk?.value, noteLockKey: i.noteLockKey ?? null,
  revisionServiceEnabled: i.revisionServiceEnabled?.value ?? null, revisionServiceTtlDays: i.revisionServiceTtlDays?.value ?? null,
  appMinVersion: i.appMinVersion || "0.0.0"});

// the shared settings this profile holds: one newer than the target's would win in Joplin's merge and reach every device
function localInfo() {
  let out = ""; try { out = joplin(["config", "syncInfoCache"]); } catch (e) { return null; }
  const m = out.match(/syncInfoCache = ([\s\S]*)$/);
  try { return m && m[1].trim() ? JSON.parse(m[1]) : null; } catch (e) { return null; }
}
function wouldPush(local, remote) {
  if (!local) return [];
  const ts = (i, k) => i?.[k]?.updatedTime || 0, out = [];
  for (const k of ["e2ee", "ppk", "revisionServiceEnabled", "revisionServiceTtlDays", "activeMasterKeyId"])
    if (ts(local, k) > ts(remote, k) && JSON.stringify(local[k]?.value ?? null) !== JSON.stringify(remote[k]?.value ?? null))
      out.push(`${k} → ${JSON.stringify(local[k]?.value ?? null)}`);
  if ((local.masterKeys || []).some(mk => !(remote.masterKeys || []).some(r => r.id === mk.id))) out.push("an encryption key");
  if ((local.version || 0) > (remote.version || 0)) out.push("format " + local.version);
  return out;
}

async function guard(when) {
  const raw = await remoteInfo(), now = shared(raw);
  if (when === "show") { console.log(JSON.stringify({target: now, cli: ownMinVersion()})); return; }
  if (when === "before") {
    if (now.version !== 3) die(3, "the sync target is format " + now.version + ", this client knows 3: nothing done (never --upgrade)");
    if (now.e2ee) die(3, "the sync target is end-to-end encrypted: this client can't read it and stays off");
    if (now.noteLockKey) die(3, "the sync target has locked notes: this client stays off");
    const push = wouldPush(localInfo(), raw);
    if (push.length) die(3, "syncing would change what your devices share: " + push.join(", ") + ". This client's profile holds a" +
      " setting it should not have: nothing synced. Start it again from an empty profile (delete state/joplin/profile)");
    const mine = ownMinVersion();
    if (vcmp(mine, now.appMinVersion) > 0 && env.JOPLIN_ALLOW_RAISE !== "1")
      die(3, `syncing would raise the apps' minimum version from ${now.appMinVersion} to ${mine}: Joplin apps older than ${mine}` +
        ` would then refuse to sync. Update your Joplin apps to ${mine} or later and sync one of them (the target then says ${mine}),` +
        ` or build this client with an older Joplin CLI (JOPLIN_VERSION)`);
    fs.writeFileSync(SNAP, JSON.stringify(now));
    return;
  }
  let was; try { was = JSON.parse(fs.readFileSync(SNAP, "utf8")); } catch (e) { die(2, "guard after without guard before"); }
  const changed = Object.keys(now).filter(k => JSON.stringify(now[k]) !== JSON.stringify(was[k]));
  if (changed.length) stop("the sync target's shared settings changed during this client's sync: " +
    changed.map(k => `${k} ${JSON.stringify(was[k])} → ${JSON.stringify(now[k])}`).join(", ") +
    ". If one of your own devices changed it at that moment (an update, a setting), all is well");
}

// --------------------------------------------------------------------- the notes
function folders(d) {
  const f = new Map(d.prepare("SELECT id, title, parent_id FROM folders WHERE deleted_time = 0 AND encryption_applied = 0").all().map(r => [r.id, r]));
  return id => { const out = [], seen = new Set(); while (f.has(id) && !seen.has(id)) { seen.add(id); out.unshift(f.get(id).title); id = f.get(id).parent_id; } return out; };
}
function notes() {
  const d = db(), pathOf = folders(d);
  // a conflict copy (an edit made on a device that synced after this client changed the note) comes too: its own
  // lines are the owner's; media-library takes them but never changes the copy (the owner deletes it in Joplin)
  const rows = d.prepare("SELECT id, title, body, parent_id, updated_time, is_conflict, conflict_original_id FROM notes" +
    " WHERE deleted_time = 0 AND encryption_applied = 0").all();
  const out = rows.filter(n => TITLES.includes(n.title.trim().toLowerCase())).map(n => ({id: n.id, title: n.title, body: n.body,
    notebook: pathOf(n.parent_id).join(" / "), updated_time: new Date(n.updated_time).toISOString(),
    ...(n.is_conflict ? {conflict_of: n.conflict_original_id || ""} : {})}))
    .filter(n => !NOTEBOOK || n.notebook.toLowerCase().split(" / ").includes(NOTEBOOK));
  d.close();
  let client = ""; try { client = joplin(["version"]).split("\n").find(l => /^joplin \d/.test(l)) || ""; } catch (e) {}
  console.log(JSON.stringify({client: client.trim(), notes: out}));
}

// ------------------------------------------------------------- deleting lines
const rowOf = id => { const d = db(); const r = d.prepare("SELECT body, updated_time, is_conflict FROM notes WHERE id = ? AND deleted_time = 0").get(id); d.close(); return r || null; };
const bodyOf = id => rowOf(id)?.body ?? null;
function strip() {
  const plan = JSON.parse(fs.readFileSync(0, "utf8") || "{}"), report = [];
  for (const n of plan.notes || []) {
    if (!/^[0-9a-f]{32}$/.test(n.id || "")) continue;
    const row = rowOf(n.id), before = row?.is_conflict ? null : row?.body;
    if (before == null) { report.push({id: n.id, removed: 0, note: "gone"}); continue; }
    const age = Date.now() - row.updated_time;
    if (age < QUIET_MS) { report.push({id: n.id, removed: 0, note: `edited ${Math.round(age / 60e3)} min ago: next time`}); continue; }
    const lines = before.split("\n"); let removed = 0;
    for (const want of n.remove || []) {
      let k = lines.findIndex(l => l === want);
      if (k < 0) k = lines.findIndex(l => l.trim() === String(want).trim());
      if (k >= 0) { lines.splice(k, 1); removed++; }       // the owner may have deleted or changed it meanwhile: then not
    }
    const after = lines.join("\n");
    if (!removed || after === before) { report.push({id: n.id, removed: 0}); continue; }
    joplin(["set", n.id, "body", "--", after]);              // "--": a body that starts with "-" is not an option
    if (bodyOf(n.id) !== after) {
      joplin(["set", n.id, "body", "--", before]);
      if (bodyOf(n.id) !== before) stop("note " + n.id + " was not stored as sent and could not be put back: it is not synced; look at it in this client's profile");
      die(5, "the Joplin CLI did not store note " + n.id + " as sent: put back as it was, nothing synced");
    }
    report.push({id: n.id, removed});
  }
  console.error(JSON.stringify({removed: report}));
}

const [cmd, arg] = process.argv.slice(2);
if (cmd !== "guard" && fs.existsSync(STOP)) die(9, "stopped: " + fs.readFileSync(STOP, "utf8").trim());
if (cmd === "setup") setup();
else if (cmd === "guard") await guard(arg);
else if (cmd === "notes") notes();
else if (cmd === "strip") strip();
else die(2, "usage: lists.mjs setup | guard before|after|show | notes | strip");
