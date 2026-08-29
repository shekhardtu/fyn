import { createContext, useContext, type ReactNode } from "react";

export type UserDefaults = {
  currency: string;
  timeZone?: string;
};

const UserDefaultsContext = createContext<UserDefaults>({ currency: "INR" });

/** Formatting defaults from the signed-in profile.
 *
 *  The fallback deliberately preserves the old browser-local behavior for
 *  isolated components and tests. Inside the workspace, profile settings are
 *  authoritative for currency fallbacks and wall-clock presentation. */
export function UserDefaultsProvider({ value, children }: { value: UserDefaults; children: ReactNode }) {
  return <UserDefaultsContext.Provider value={value}>{children}</UserDefaultsContext.Provider>;
}

export function useUserDefaults() {
  return useContext(UserDefaultsContext);
}
