/**
 * The Cloudflare Worker that fronts the FastAPI container.
 *
 * Every request to the Worker is forwarded, unchanged, to the container on
 * port 8080. The container is a Durable Object, which is what makes
 * `max_instances: 1` in wrangler.jsonc meaningful: ONE instance, so
 * MitraSessionManager's in-process WebSocket pool stays single-owner, exactly
 * as the "never run more than one uvicorn worker" rule requires.
 */
import { Container, getContainer } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

export class SaarathiBackend extends Container {
  // Must match EXPOSE/PORT in the Dockerfile.
  defaultPort = 8080;

  // How long an idle container stays up. A sleep drops the in-memory Mitra
  // channels, which is recoverable -- re-authenticating with the same
  // remote_session_id resumes the interview (see session_manager.py) -- but it
  // costs a reconnect, so keep this comfortably longer than a user's think time.
  sleepAfter = "30m";

  // Worker secrets and vars, handed to the process as real environment
  // variables. These outrank .env by design (app/core/settings.py), and there
  // is no .env in the image at all.
  envVars = {
    APP_ENV: "production",
    HOST: "0.0.0.0",
    PORT: "8080",
    // MITRA_ENABLED=1 requires exactly one worker; assert_single_worker()
    // aborts startup otherwise.
    WORKERS: "1",
    API_PREFIX: env.API_PREFIX,
    LOG_LEVEL: "INFO",

    DATABASE_URL: env.DATABASE_URL,
    DB_POOL_SIZE: env.DB_POOL_SIZE,
    DB_MAX_OVERFLOW: env.DB_MAX_OVERFLOW,
    THREADPOOL_SIZE: env.THREADPOOL_SIZE,

    OPENROUTER_API_KEY: env.OPENROUTER_API_KEY,
    OPENROUTER_MODEL: env.OPENROUTER_MODEL,
    LLM_TIMEOUT: env.LLM_TIMEOUT,
    LLM_MAX_RETRIES: env.LLM_MAX_RETRIES,

    // Must list the deployed frontend origin or every browser request is
    // blocked by CORS before it reaches a route.
    FRONTEND_ORIGINS: env.FRONTEND_ORIGINS,

    AUTH_CHECK: env.AUTH_CHECK,
    SAARTHI_STATIC_TOKEN: env.SAARTHI_STATIC_TOKEN,
    JWT_IDENTIFIER_FIELD: env.JWT_IDENTIFIER_FIELD,
    JWT_EMAIL_SUFFIX: env.JWT_EMAIL_SUFFIX,
    SAARTHI_ADMIN_ENABLED: env.SAARTHI_ADMIN_ENABLED,

    MITRA_ENABLED: env.MITRA_ENABLED,
    // Origin is a credential Mitra gates admission on -- a secret, never a var.
    MITRA_ORIGIN_URL: env.MITRA_ORIGIN_URL,
    MITRA_MAX_OPEN_CHANNELS: env.MITRA_MAX_OPEN_CHANNELS,
    MITRA_IDLE_CLOSE_S: env.MITRA_IDLE_CLOSE_S,
  };

  override onStart() {
    console.log("saarathi backend container started");
  }

  override onError(error: unknown) {
    console.log("saarathi backend container error:", error);
  }
}

export default {
  async fetch(request: Request, workerEnv: any): Promise<Response> {
    // No id argument: one shared instance for the whole deployment.
    return getContainer(workerEnv.BACKEND).fetch(request);
  },
};
