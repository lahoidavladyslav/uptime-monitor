import { useMutation, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import { ME_KEY } from "@/app/queryClient";

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { username: string; password: string }) => unwrap(client.POST("/api/v1/auth/login", { body })),
    // Once the session exists, refetching `me` makes <PublicOnly> redirect to ?next= (or /).
    onSuccess: (res) => {
      if (res.status === "ok") void qc.invalidateQueries({ queryKey: ME_KEY });
    },
  });
}

export function useVerify2fa() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { code: string }) => unwrap(client.POST("/api/v1/auth/login/2fa", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ME_KEY }),
  });
}

export function useSignup() {
  return useMutation({
    mutationFn: (body: { username: string; email: string; password1: string; password2: string }) =>
      unwrap(client.POST("/api/v1/auth/signup", { body })),
  });
}

export function useConfirm() {
  return useMutation({ mutationFn: (token: string) => unwrap(client.POST("/api/v1/auth/confirm", { body: { token } })) });
}

export function useResend() {
  return useMutation({ mutationFn: (email: string) => unwrap(client.POST("/api/v1/auth/resend", { body: { email } })) });
}

export function usePasswordReset() {
  return useMutation({
    mutationFn: (email: string) => unwrap(client.POST("/api/v1/auth/password-reset", { body: { email } })),
  });
}

export function usePasswordResetConfirm() {
  return useMutation({
    mutationFn: (body: { uid: string; token: string; new_password1: string; new_password2: string }) =>
      unwrap(client.POST("/api/v1/auth/password-reset/confirm", { body })),
  });
}
