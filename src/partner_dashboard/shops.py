import psycopg


def upsert_shop_state(conn: psycopg.Connection, shop_gid: str, *,
                       install_state: str, at, shop_domain=None, shop_name=None,
                       plan_monthly=None, uninstall_reason=None,
                       uninstall_description=None) -> None:
    """Upsert install-state on a shop row without clobbering identity fields.

    shop_domain/shop_name arrive on every Partner API event, so they're
    coalesced in (event value wins only when the column is empty; a CSV
    import or later event may have filled it already). The remaining identity
    fields (owner_name, email, country, industry) come only from the CSV
    importer and must never appear in the do-update clause here.
    """
    uninstalled_at = at if install_state == "uninstalled" else None
    conn.execute(
        """
        insert into shops (shop_gid, shop_domain, shop_name, install_state,
                           installed_at, uninstalled_at,
                           uninstall_reason, uninstall_description)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (shop_gid) do update set
            shop_domain = coalesce(shops.shop_domain, excluded.shop_domain),
            shop_name = coalesce(shops.shop_name, excluded.shop_name),
            install_state = excluded.install_state,
            installed_at = coalesce(shops.installed_at, excluded.installed_at),
            uninstalled_at = case when excluded.install_state = 'uninstalled'
                                  then excluded.uninstalled_at
                                  else shops.uninstalled_at end,
            -- Latest uninstall wins, but a reinstall must not wipe the feedback
            -- the merchant already gave.
            uninstall_reason = case when excluded.install_state = 'uninstalled'
                                    then coalesce(excluded.uninstall_reason,
                                                  shops.uninstall_reason)
                                    else shops.uninstall_reason end,
            uninstall_description = case when excluded.install_state = 'uninstalled'
                                         then coalesce(excluded.uninstall_description,
                                                       shops.uninstall_description)
                                         else shops.uninstall_description end,
            updated_at = now()
        """,
        (shop_gid, shop_domain, shop_name, install_state, at, uninstalled_at,
         uninstall_reason, uninstall_description),
    )
