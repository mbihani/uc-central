import { createContext, useContext, type ReactNode } from "react";
import { useCheckIsAdmin } from "@/lib/api";
import selector from "@/lib/selector";

interface AdminContextValue {
  isAdmin: boolean;
  isLoading: boolean;
}

const AdminContext = createContext<AdminContextValue>({
  isAdmin: false,
  isLoading: true,
});

export function AdminProvider({ children }: { children: ReactNode }) {
  const { data, isLoading } = useCheckIsAdmin(selector());

  const value: AdminContextValue = {
    isAdmin: data?.is_admin ?? false,
    isLoading,
  };

  return (
    <AdminContext.Provider value={value}>{children}</AdminContext.Provider>
  );
}

export function useAdmin(): AdminContextValue {
  return useContext(AdminContext);
}
