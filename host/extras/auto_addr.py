"""Preserve native discovery without rewriting an unchanged device table."""
from . import auto_addr_wrapper


def table_uniids(table):
    return tuple(tuple(item.uniid)
                 for item in table.addr_manager_table[:table.size])


def load_config(config):
    address = auto_addr_wrapper.AutoAddrWrapper(config)
    saved = {}
    for table in auto_addr_wrapper.dev_table_map_table:
        name = auto_addr_wrapper.name_map[table.dev_type]
        if config.get(name, None, note_valid=False) is not None:
            saved[table.dev_type] = table_uniids(table)
    original_save = address.save_addr_table_uniids

    def save_if_changed(table):
        snapshot = table_uniids(table)
        if saved.get(table.dev_type) == snapshot:
            return
        result = original_save(table)
        # Cache only successful persistence, not mutable discovery objects.
        saved[table.dev_type] = snapshot
        return result

    address.save_addr_table_uniids = save_if_changed
    return address
