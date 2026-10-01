import { NavLink, type NavLinkProps } from "@mantine/core";
import type { MouseEventHandler, Ref } from "react";

interface Props extends Omit<NavLinkProps, "href"> {
  href?: string;
  onClick?: MouseEventHandler<HTMLAnchorElement>;
  ref?: Ref<HTMLAnchorElement>;
}

export function MantineNavLink(props: Props) {
  return <NavLink component="a" {...props} />;
}
