import axios from "axios";
export const wb = axios.create({
  baseURL: `${location.origin}/workbench`,
  timeout: 30000,
});
export const messageOf = (error: unknown): string =>
  axios.isAxiosError(error)
    ? String(error.response?.data?.detail || error.message)
    : String(error);
