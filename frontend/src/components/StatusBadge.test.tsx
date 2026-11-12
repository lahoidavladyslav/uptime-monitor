import { render, screen } from "@testing-library/react";

import { t } from "@/lib/strings";

import { StatusBadge, type Status } from "./StatusBadge";

const ALL: Status[] = ["up", "down", "unknown", "maintenance", "paused", "degraded"];

describe("StatusBadge", () => {
  it.each(ALL)("%s renders Ukrainian text AND an icon (colour is never the only signal)", (status) => {
    const { container } = render(<StatusBadge status={status} />);
    expect(screen.getByText(t.status[status])).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.firstElementChild).toHaveAttribute("data-status", status);
  });

  it("uses a different icon per status", () => {
    const paths = ALL.map((s) => render(<StatusBadge status={s} />).container.querySelector("svg")?.innerHTML);
    expect(new Set(paths).size).toBe(ALL.length);
  });
});
