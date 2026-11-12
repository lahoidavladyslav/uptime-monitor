import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { EmptyState, ErrorState, ListSkeleton } from "./States";

describe("States", () => {
  it("ErrorState shows the message and calls onRetry", async () => {
    const onRetry = vi.fn();
    render(<ErrorState message="Сервер лежить" onRetry={onRetry} />);
    expect(screen.getByText("Сервер лежить")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Повторити" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("EmptyState renders title, description and an optional action", () => {
    render(<EmptyState title="Порожньо" description="Додайте перший монітор" action={<button>Додати</button>} />);
    expect(screen.getByText("Порожньо")).toBeInTheDocument();
    expect(screen.getByText("Додайте перший монітор")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Додати" })).toBeInTheDocument();
  });

  it("ListSkeleton is announced as busy and renders the requested rows", () => {
    render(<ListSkeleton rows={4} />);
    const box = screen.getByRole("status", { name: "Завантаження" });
    expect(box).toHaveAttribute("aria-busy", "true");
    expect(box.querySelectorAll("[data-skeleton-row]")).toHaveLength(4);
  });
});
