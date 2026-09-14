import { z } from "zod";
export const Id = z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/);
export const Subject = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[^\u0000-\u001f\u007f]+$/);
