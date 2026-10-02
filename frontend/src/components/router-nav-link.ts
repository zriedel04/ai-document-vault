import { createLink } from "@tanstack/react-router";

import { MantineNavLink } from "./MantineNavLink";

/** A Mantine navigation link that routes through TanStack Router. */
export const RouterNavLink = createLink(MantineNavLink);
