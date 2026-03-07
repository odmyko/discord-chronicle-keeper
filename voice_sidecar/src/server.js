import express from "express";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import {
  Client,
  GatewayIntentBits,
  ChannelType,
} from "discord.js";
import {
  EndBehaviorType,
  VoiceConnectionStatus,
  entersState,
  joinVoiceChannel,
} from "@discordjs/voice";
import prism from "prism-media";

const app = express();
app.use(express.json({ limit: "256kb" }));

const port = Number(process.env.SIDECAR_PORT || "8081");
const dataDir = process.env.DATA_DIR || "/app/data";
const sidecarToken = (process.env.SIDECAR_TOKEN || "").trim();
const discordToken = (process.env.DISCORD_BOT_TOKEN || "").trim();
const runtimePath = path.join(dataDir, "runtime", "voice_sidecar_state.json");
const sidecarDaveEncryption = (
  process.env.SIDECAR_DAVE_ENCRYPTION || "true"
).trim().toLowerCase() === "true";
const sidecarDecryptionFailureToleranceRaw = (
  process.env.SIDECAR_DECRYPTION_FAILURE_TOLERANCE || ""
).trim();
const sidecarDecryptionFailureTolerance = Number.parseInt(
  sidecarDecryptionFailureToleranceRaw,
  10,
);

const PCM_SAMPLE_RATE = 48000;
const PCM_CHANNELS = 2;
const PCM_BITS = 16;
const PCM_BLOCK_ALIGN = (PCM_CHANNELS * PCM_BITS) / 8;
const PCM_BYTE_RATE = PCM_SAMPLE_RATE * PCM_BLOCK_ALIGN;

const sessions = new Map(); // guildId -> serializable state
const runtimeSessions = new Map(); // guildId -> live runtime resources

const discordClient = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildVoiceStates,
    GatewayIntentBits.GuildMembers,
  ],
});
let discordReady = false;

class WavFileWriter {
  constructor(filePath) {
    this.filePath = filePath;
    this.fd = fs.openSync(filePath, "w");
    this.bytesWritten = 0;
    this.closed = false;
    fs.writeSync(this.fd, Buffer.alloc(44));
  }

  write(buffer) {
    if (this.closed || !buffer || buffer.length === 0) {
      return;
    }
    fs.writeSync(this.fd, buffer);
    this.bytesWritten += buffer.length;
  }

  close() {
    if (this.closed) {
      return;
    }
    const header = Buffer.alloc(44);
    header.write("RIFF", 0, "ascii");
    header.writeUInt32LE(36 + this.bytesWritten, 4);
    header.write("WAVE", 8, "ascii");
    header.write("fmt ", 12, "ascii");
    header.writeUInt32LE(16, 16);
    header.writeUInt16LE(1, 20); // PCM
    header.writeUInt16LE(PCM_CHANNELS, 22);
    header.writeUInt32LE(PCM_SAMPLE_RATE, 24);
    header.writeUInt32LE(PCM_BYTE_RATE, 28);
    header.writeUInt16LE(PCM_BLOCK_ALIGN, 32);
    header.writeUInt16LE(PCM_BITS, 34);
    header.write("data", 36, "ascii");
    header.writeUInt32LE(this.bytesWritten, 40);
    fs.writeSync(this.fd, header, 0, 44, 0);
    fs.closeSync(this.fd);
    this.closed = true;
  }
}

function sanitizeName(value) {
  const text = String(value || "").trim();
  const withUnderscores = text.replace(/\s+/g, "_");
  const safe = withUnderscores.replace(/[^A-Za-z0-9_.-]/g, "");
  return safe || "unknown";
}

function normalizeSnowflake(value, fieldName) {
  const text = String(value ?? "").trim();
  if (!/^\d{15,22}$/.test(text)) {
    return { error: `${fieldName} must be a valid Discord snowflake` };
  }
  return { value: text };
}

function utcSessionId() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}` +
    `_${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}`
  );
}

function requireToken(req, res, next) {
  if (!sidecarToken) {
    return next();
  }
  const provided = (req.header("x-sidecar-token") || "").trim();
  if (provided !== sidecarToken) {
    return res.status(401).json({ error: "unauthorized" });
  }
  return next();
}

async function persistState() {
  const payload = {
    updated_at_utc: new Date().toISOString(),
    sessions: Object.fromEntries(sessions),
  };
  await fsp.mkdir(path.dirname(runtimePath), { recursive: true });
  await fsp.writeFile(runtimePath, JSON.stringify(payload, null, 2), "utf-8");
}

function normalizeStartBody(body) {
  const guildIdResult = normalizeSnowflake(body?.guild_id, "guild_id");
  if (guildIdResult.error) {
    return { error: guildIdResult.error };
  }
  const voiceChannelResult = normalizeSnowflake(
    body?.voice_channel_id,
    "voice_channel_id",
  );
  if (voiceChannelResult.error) {
    return { error: voiceChannelResult.error };
  }

  return {
    guild_id: guildIdResult.value,
    voice_channel_id: voiceChannelResult.value,
    text_channel_id: String(body?.text_channel_id || "").trim() || null,
    requested_by: String(body?.requested_by || "").trim() || null,
    campaign_id: String(body?.campaign_id || ""),
    campaign_name: String(body?.campaign_name || ""),
    summary_language: String(body?.summary_language || "ru"),
    session_context: String(body?.session_context || ""),
    name_hints: String(body?.name_hints || ""),
    session_id: String(body?.session_id || "").trim() || utcSessionId(),
  };
}

function normalizeGuildBody(body) {
  const guildIdResult = normalizeSnowflake(body?.guild_id, "guild_id");
  if (guildIdResult.error) {
    return { error: guildIdResult.error };
  }
  return { guild_id: guildIdResult.value, reason: String(body?.reason || "") };
}

function currentSessionPath(guildId, sessionId) {
  return path.join(dataDir, "sessions", String(guildId), sessionId);
}

function buildTrackPath(audioDir, speakerName, userId, segmentIndex) {
  const segment = String(segmentIndex).padStart(3, "0");
  const base = `${sanitizeName(speakerName)}_${userId}_seg${segment}`;
  return path.join(audioDir, `${base}.wav`);
}

async function ensureDiscordReady() {
  if (!discordToken) {
    throw new Error("DISCORD_BOT_TOKEN is required for sidecar recording.");
  }
  if (discordReady) {
    return;
  }
  await new Promise((resolve, reject) => {
    const onReady = () => {
      discordReady = true;
      cleanup();
      resolve();
    };
    const onError = (err) => {
      cleanup();
      reject(err);
    };
    const cleanup = () => {
      discordClient.off("ready", onReady);
      discordClient.off("error", onError);
    };
    discordClient.once("ready", onReady);
    discordClient.once("error", onError);
    discordClient.login(discordToken).catch(onError);
  });
}

function markSegmentData(guildId) {
  const runtime = runtimeSessions.get(guildId);
  if (runtime) {
    runtime.currentSegmentHasData = true;
  }
}

function closeActiveStream(entry) {
  if (!entry) {
    return;
  }
  try {
    entry.opusStream?.destroy();
  } catch (_err) {}
  try {
    entry.pcmStream?.destroy();
  } catch (_err) {}
}

function finalizeSegment(guildId) {
  const key = guildId;
  const runtime = runtimeSessions.get(key);
  const state = sessions.get(key);
  if (!runtime || !state) {
    return;
  }
  for (const active of runtime.activeStreams.values()) {
    closeActiveStream(active);
  }
  runtime.activeStreams.clear();
  for (const writerEntry of runtime.userWriters.values()) {
    try {
      writerEntry.writer.close();
    } catch (_err) {}
  }
  runtime.userWriters.clear();
  if (runtime.currentSegmentHasData) {
    state.segments_written += 1;
  }
  runtime.currentSegmentHasData = false;
  runtime.segmentIndex += 1;
}

async function stopRuntimeSession(guildId, reason) {
  const key = guildId;
  const runtime = runtimeSessions.get(key);
  const session = sessions.get(key);
  if (!session) {
    throw new Error("session not found");
  }

  if (runtime) {
    try {
      runtime.receiver?.speaking?.off("start", runtime.onSpeakingStart);
    } catch (_err) {}
    finalizeSegment(guildId);
    try {
      runtime.connection?.destroy();
    } catch (_err) {}
    runtimeSessions.delete(key);
  }

  session.status = "stopped";
  session.stopped_at_utc = new Date().toISOString();
  session.stop_reason = reason || "manual";
  await persistState();
  return session;
}

async function resolveSpeakerName(guild, userId) {
  const cached = guild.members.cache.get(userId);
  if (cached?.displayName) {
    return cached.displayName;
  }
  try {
    const member = await guild.members.fetch(userId);
    if (member?.displayName) {
      return member.displayName;
    }
  } catch (_err) {}
  return `user_${userId}`;
}

async function createOrGetWriter(guild, runtime, userId) {
  let writerEntry = runtime.userWriters.get(userId);
  if (writerEntry) {
    return writerEntry;
  }
  const speakerName = await resolveSpeakerName(guild, userId);
  const outPath = buildTrackPath(
    runtime.audioDir,
    speakerName,
    userId,
    runtime.segmentIndex,
  );
  const writer = new WavFileWriter(outPath);
  writerEntry = { writer, outPath, speakerName, userId };
  runtime.userWriters.set(userId, writerEntry);
  return writerEntry;
}

function attachSpeakingHandler(guild, runtime) {
  const onSpeakingStart = async (userId) => {
    if (!runtime || runtime.stopped) {
      return;
    }
    if (runtime.activeStreams.has(userId)) {
      return;
    }
    try {
      const writerEntry = await createOrGetWriter(guild, runtime, userId);
      const opusStream = runtime.receiver.subscribe(userId, {
        end: {
          behavior: EndBehaviorType.AfterSilence,
          duration: 1000,
        },
      });
      const pcmStream = new prism.opus.Decoder({
        frameSize: 960,
        channels: PCM_CHANNELS,
        rate: PCM_SAMPLE_RATE,
      });
      runtime.activeStreams.set(userId, { opusStream, pcmStream });

      opusStream.pipe(pcmStream);
      pcmStream.on("data", (chunk) => {
        if (chunk && chunk.length > 0) {
          writerEntry.writer.write(chunk);
          markSegmentData(runtime.guildId);
        }
      });
      const finalize = () => {
        const active = runtime.activeStreams.get(userId);
        closeActiveStream(active);
        runtime.activeStreams.delete(userId);
      };
      opusStream.on("error", finalize);
      pcmStream.on("error", finalize);
      opusStream.on("end", finalize);
      pcmStream.on("end", finalize);
      opusStream.on("close", finalize);
      pcmStream.on("close", finalize);
    } catch (err) {
      console.error("[voice-sidecar] speaker stream failed:", err);
    }
  };

  runtime.onSpeakingStart = onSpeakingStart;
  runtime.receiver.speaking.on("start", onSpeakingStart);
}

app.get("/health", (_req, res) => {
  const running = [...sessions.values()].filter(
    (s) => s.status === "recording",
  ).length;
  res.json({
    ok: true,
    service: "chronicle-voice-sidecar",
    mode: "live",
    discord_ready: discordReady,
    sessions_running: running,
  });
});

app.get("/v1/status", (_req, res) => {
  res.json({
    ok: true,
    mode: "live",
    sessions: Object.fromEntries(sessions),
  });
});

app.get("/v1/sessions/:guildId/status", (req, res) => {
  const guildIdResult = normalizeSnowflake(req.params.guildId, "guild_id");
  if (guildIdResult.error) {
    return res.status(400).json({ error: guildIdResult.error });
  }
  const guildId = guildIdResult.value;
  const key = guildId;
  const session = sessions.get(key);
  if (!session) {
    return res.status(404).json({ error: "session not found", guild_id: guildId });
  }
  return res.json({ ok: true, session });
});

app.post("/v1/sessions/start", requireToken, async (req, res) => {
  const payload = normalizeStartBody(req.body);
  if (payload.error) {
    return res.status(400).json({ error: payload.error });
  }
  const key = payload.guild_id;
  const existing = sessions.get(key);
  if (existing && existing.status === "recording") {
    if (existing.voice_channel_id === payload.voice_channel_id) {
      return res.json({
        ok: true,
        idempotent: true,
        session: existing,
      });
    }
    return res.status(409).json({
      error: "session already running for guild",
      running_session: existing,
    });
  }

  try {
    await ensureDiscordReady();
    const guild = await discordClient.guilds.fetch(payload.guild_id);
    if (!guild) {
      return res.status(404).json({ error: "guild not found" });
    }
    const channel = await guild.channels.fetch(payload.voice_channel_id);
    if (
      !channel
      || (
        channel.type !== ChannelType.GuildVoice
        && channel.type !== ChannelType.GuildStageVoice
      )
    ) {
      return res
        .status(400)
        .json({
          error:
            "voice_channel_id must point to a guild voice/stage voice channel",
        });
    }
    const sessionDir = currentSessionPath(payload.guild_id, payload.session_id);
    const audioDir = path.join(sessionDir, "audio");
    await fsp.mkdir(audioDir, { recursive: true });

    const connectDebug = [];
    const pushConnectDebug = (message) => {
      const ts = new Date().toISOString();
      connectDebug.push(`${ts} ${message}`);
      if (connectDebug.length > 10) {
        connectDebug.shift();
      }
    };
    const connection = joinVoiceChannel({
      channelId: payload.voice_channel_id,
      guildId: payload.guild_id,
      adapterCreator: guild.voiceAdapterCreator,
      selfDeaf: false,
      selfMute: true,
      daveEncryption: sidecarDaveEncryption,
      ...(Number.isFinite(sidecarDecryptionFailureTolerance)
        ? { decryptionFailureTolerance: sidecarDecryptionFailureTolerance }
        : {}),
    });
    pushConnectDebug(
      `join opts daveEncryption=${sidecarDaveEncryption} decryptionFailureTolerance=${
        Number.isFinite(sidecarDecryptionFailureTolerance)
          ? sidecarDecryptionFailureTolerance
          : "default"
      }`,
    );
    const onStateChange = (oldState, newState) => {
      pushConnectDebug(`state ${oldState.status} -> ${newState.status}`);
      const oldNetworking = Reflect.get(oldState, "networking");
      const newNetworking = Reflect.get(newState, "networking");
      if (oldNetworking === newNetworking) {
        return;
      }
      if (oldNetworking?.off && oldNetworking.__dbgStateHandler) {
        oldNetworking.off("stateChange", oldNetworking.__dbgStateHandler);
      }
      if (newNetworking?.on) {
        const networkHandler = (oldNet, newNet) => {
          pushConnectDebug(`network ${oldNet.code} -> ${newNet.code}`);
          const ws = Reflect.get(newNet, "ws");
          if (ws && !ws.__dbgCloseHooked) {
            ws.__dbgCloseHooked = true;
            ws.on("close", (code, reason) => {
              const extractCode = (value) => {
                if (typeof value === "number" || typeof value === "string") {
                  return String(value);
                }
                if (value && typeof value === "object") {
                  const candidate = (
                    value.code
                    ?? value.closeCode
                    ?? value.statusCode
                    ?? value.type
                  );
                  if (candidate !== undefined && candidate !== null) {
                    return String(candidate);
                  }
                  try {
                    return JSON.stringify(value);
                  } catch (_err) {
                    return String(value);
                  }
                }
                return String(value ?? "");
              };
              let reasonText = "";
              try {
                reasonText = Buffer.isBuffer(reason)
                  ? reason.toString("utf-8")
                  : String(reason || "");
              } catch (_err) {
                reasonText = "";
              }
              pushConnectDebug(
                `ws close code=${extractCode(code)} reason=${reasonText}`,
              );
            });
          }
        };
        newNetworking.__dbgStateHandler = networkHandler;
        newNetworking.on("stateChange", networkHandler);
      }
    };
    const onError = (err) => {
      pushConnectDebug(`error ${String(err)}`);
    };
    connection.on("stateChange", onStateChange);
    connection.on("error", onError);
    try {
      await entersState(connection, VoiceConnectionStatus.Ready, 20_000);
    } catch (err) {
      try {
        connection.destroy();
      } catch (_destroyErr) {}
      const daveRequired = connectDebug.some((line) =>
        line.includes("ws close code=4017"),
      );
      if (daveRequired) {
        throw new Error(
          "voice_connect_failed: Discord voice requires DAVE/E2EE (close code 4017), which this recorder stack cannot negotiate yet.",
        );
      }
      let extra = "";
      if (connectDebug.length > 0) {
        extra = ` debug=[${connectDebug.join(" | ")}]`;
      }
      throw new Error(`voice_connect_timeout: ${String(err)}${extra}`);
    } finally {
      connection.off("stateChange", onStateChange);
      connection.off("error", onError);
    }

    const session = {
      guild_id: payload.guild_id,
      voice_channel_id: payload.voice_channel_id,
      text_channel_id: payload.text_channel_id,
      requested_by: payload.requested_by,
      campaign_id: payload.campaign_id,
      campaign_name: payload.campaign_name,
      summary_language: payload.summary_language,
      session_context: payload.session_context,
      name_hints: payload.name_hints,
      session_id: payload.session_id,
      session_dir: sessionDir,
      started_at_utc: new Date().toISOString(),
      status: "recording",
      segments_written: 0,
      backend: "discordjs-voice",
    };
    const runtime = {
      guildId: payload.guild_id,
      audioDir,
      connection,
      receiver: connection.receiver,
      activeStreams: new Map(),
      userWriters: new Map(),
      segmentIndex: 1,
      currentSegmentHasData: false,
      onSpeakingStart: null,
      stopped: false,
    };
    attachSpeakingHandler(guild, runtime);
    sessions.set(key, session);
    runtimeSessions.set(key, runtime);
    await persistState();
    return res.status(201).json({
      ok: true,
      session,
    });
  } catch (err) {
    console.error("[voice-sidecar] start failed:", err);
    return res.status(500).json({ error: `start_failed: ${String(err)}` });
  }
});

app.post("/v1/sessions/rotate", requireToken, async (req, res) => {
  const payload = normalizeGuildBody(req.body);
  if (payload.error) {
    return res.status(400).json({ error: payload.error });
  }
  const key = payload.guild_id;
  const session = sessions.get(key);
  if (!session) {
    return res.status(404).json({ error: "session not found" });
  }
  if (session.status !== "recording") {
    return res.status(409).json({ error: "session is not recording", session });
  }
  const runtime = runtimeSessions.get(key);
  if (!runtime) {
    return res.status(409).json({ error: "runtime session missing", session });
  }

  finalizeSegment(payload.guild_id);
  session.segments_written = Math.max(
    Number(session.segments_written || 0),
    runtime.segmentIndex - 1,
  );
  session.last_rotation_at_utc = new Date().toISOString();
  session.last_rotation_reason = payload.reason || "manual";
  await persistState();
  return res.json({ ok: true, session });
});

app.post("/v1/sessions/stop", requireToken, async (req, res) => {
  const payload = normalizeGuildBody(req.body);
  if (payload.error) {
    return res.status(400).json({ error: payload.error });
  }
  const key = payload.guild_id;
  const session = sessions.get(key);
  if (!session) {
    return res.status(404).json({ error: "session not found" });
  }
  if (session.status === "stopped") {
    return res.json({ ok: true, idempotent: true, session });
  }
  try {
    const stopped = await stopRuntimeSession(payload.guild_id, payload.reason);
    return res.json({ ok: true, session: stopped });
  } catch (err) {
    return res.status(500).json({ error: `stop_failed: ${String(err)}` });
  }
});

app.use((err, _req, res, _next) => {
  console.error("[voice-sidecar] unhandled error:", err);
  res.status(500).json({ error: "internal_error" });
});

async function shutdown() {
  for (const guildId of runtimeSessions.keys()) {
    try {
      await stopRuntimeSession(Number(guildId), "shutdown");
    } catch (_err) {}
  }
  try {
    await discordClient.destroy();
  } catch (_err) {}
  process.exit(0);
}

process.on("SIGINT", () => {
  shutdown();
});
process.on("SIGTERM", () => {
  shutdown();
});

app.listen(port, async () => {
  console.log(`[voice-sidecar] listening on :${port} mode=live data_dir=${dataDir}`);
  if (!discordToken) {
    console.warn("[voice-sidecar] DISCORD_BOT_TOKEN is missing; start will fail.");
    return;
  }
  try {
    await ensureDiscordReady();
    console.log(`[voice-sidecar] discord client ready user=${discordClient.user?.tag}`);
  } catch (err) {
    console.error("[voice-sidecar] discord login failed:", err);
  }
});
