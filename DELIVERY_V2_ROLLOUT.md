# Delivery V2 rollout

## What is ready

- PostgreSQL remains the delivery source of truth for both the legacy driver app and `flutter_orders`.
- Firebase users are authorized by `users/{uid}.role`; email and legacy-user matching are not used.
- Every new scan event stores the Firebase UID, display name, role, device, client timestamp, and server timestamp.
- Drivers have route selection, pickup, explicit arrival at the main warehouse, embroidery out/back scanning, offline scan queueing, and completed history.
- Managers can search MoySklad by exact document number (`move`, `demand`, or `purchaseorder`), preview the result, autofill a delivery, create it, and receive packages.
- The existing MoySklad move-state callbacks still fire when all packages are picked or received, so the staged client migration does not remove legacy behavior.
- Supervisors can create, edit, receive, force-complete with a reason, retry MoySklad enrichment, select many deliveries, bulk edit, and permanently bulk delete.
- Completed deliveries receive `archived_at` and are returned by the completed endpoint instead of the active endpoint.
- Existing Firestore delivery screens and callable exports are disconnected. Do not delete the test Firestore documents until the first staged devices have passed acceptance testing.

## Required production configuration

Backend environment variables:

- `DATABASE_URL`: existing delivery PostgreSQL database.
- `FIREBASE_NOTIFICATION`: service-account JSON for the same Firebase project used by `flutter_orders` (`moysklad-orders`).
- `MOYSKLAD_TOKEN`: MoySklad bearer token.

Firebase `users/{uid}` profiles need:

- `role`: `driver`, `manager`, or `supervisor`.
- `name` (recommended): the human-readable name stored on scans.
- `warehouseId` (optional): a PostgreSQL warehouse UUID. When present for a manager, access is restricted to that warehouse. Missing or non-PostgreSQL IDs use role-only behavior and let the manager choose the shop.

## Deployment order

1. Back up PostgreSQL.
2. Configure the release/pre-deploy step to run `alembic upgrade head`, then start the new backend code. The migration must finish before V2 traffic reaches the service.
3. Confirm `/health` and authenticate one supervisor against `/api/v2/deliveries/warehouses`.
4. Install the new `flutter_orders` build for one supervisor and one driver.
5. Create a test delivery from a real MoySklad move/demand/purchase order; scan pickup, arrival, and completion.
6. Confirm the legacy driver app sees the same delivery counters during the staged period.
7. Expand device by device.
8. After acceptance, remove only the test delivery collections (`deliveries` including nested events, `completedDeliveries`, `driverRoutes`, `deliveryAudit`, and `deliveryImportLogs`) and deploy Cloud Functions without the old delivery exports. Do not remove the shared `users` or `warehouses` collections.

The Flutter client defaults to `https://delivery-backend-1-7v1z.onrender.com`. A different environment can be selected at build time with `--dart-define=DELIVERY_API_URL=https://...`.

## Verification performed locally

- Backend module compilation and FastAPI route loading.
- Seven focused backend unit tests for role-only authorization, request contracts, MoySklad place extraction, and item summaries.
- Dart analysis of all new delivery files and their manager/supervisor/login integration: no errors. The pre-existing dashboards still report their existing warnings.
- TypeScript validation with `tsc --noEmit`: passed. The normal local TypeScript build could not overwrite several existing generated `functions/lib` files because Windows denied write access.

## Not performed yet

- No production database migration or deployment.
- No real Firebase token, MoySklad account, barcode device, or production data was used in local tests.
- No live Firestore documents were deleted.
- The final `flutter test` command did not start within 90 seconds in this Windows workspace and was stopped cleanly. Dart analysis completed successfully; the repository's pre-change Flutter test baseline had passed earlier in this work session.
