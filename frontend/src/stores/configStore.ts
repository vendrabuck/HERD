import { create } from "zustand";

interface ConfigState {
  configToken: string | null;
  setConfigToken: (token: string) => void;
  clearConfigToken: () => void;
}

export const useConfigStore = create<ConfigState>((set) => ({
  configToken: sessionStorage.getItem("config_token"),
  setConfigToken: (token) => {
    sessionStorage.setItem("config_token", token);
    set({ configToken: token });
  },
  clearConfigToken: () => {
    sessionStorage.removeItem("config_token");
    set({ configToken: null });
  },
}));
