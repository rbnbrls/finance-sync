from finance_sync.api.v1.router import router


def test_spending_management_routes_are_registered() -> None:
    paths = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in router.routes
    }
    assert ("/spending/mappings", ("GET",)) in paths
    assert ("/spending/merchant-mappings", ("POST",)) in paths
    assert ("/transactions/{transaction_id}/override", ("POST",)) in paths
    assert ("/transactions/{transaction_id}", ("DELETE",)) in paths
    assert ("/transactions/{transaction_id}/split", ("POST",)) in paths
    assert ("/transactions/{transaction_id}/event", ("POST",)) in paths
    assert ("/spending/rules", ("GET",)) in paths
    assert ("/spending/rules", ("POST",)) in paths
    assert ("/spending/privacy-policy", ("GET",)) in paths
    assert ("/spending/privacy-policy", ("PUT",)) in paths
    assert ("/destinations/{target_id}/reconciliation", ("POST",)) in paths


def test_spending_write_routes_are_guarded_by_transactions_write_permission() -> (
    None
):
    """The new spending mutations must not bypass the auth policy."""
    write_paths = {
        "/transactions/{transaction_id}/override",
        "/transactions/{transaction_id}",
        "/transactions/{transaction_id}/split",
        "/transactions/{transaction_id}/event",
        "/spending/rules",
        "/spending/privacy-policy",
    }
    for route in router.routes:
        if route.path not in write_paths:
            continue
        methods = route.methods or set()
        if not methods.intersection({"POST", "PUT", "DELETE"}):
            continue
        permission_check = route.dependant.dependencies[0].call
        closure_values = {
            cell.cell_contents for cell in (permission_check.__closure__ or ())
        }
        assert {"transactions", "write"} <= closure_values
