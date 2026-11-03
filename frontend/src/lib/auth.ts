import { betterAuth } from "better-auth";
import { nextCookies } from "better-auth/next-js";
import { Pool } from "pg";

const databaseUrl =
  process.env.BETTER_AUTH_DATABASE_URL ||
  process.env.DATABASE_URL ||
  "postgresql://postgres:postgres@localhost:5432/travel_planning";

export const auth = betterAuth({
  database: new Pool({ connectionString: databaseUrl }),
  emailAndPassword: {
    enabled: true,
  },
  trustedOrigins: ["http://localhost:3000"],
  plugins: [nextCookies()],
});
