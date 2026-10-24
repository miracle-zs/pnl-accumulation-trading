# 0012. Pluggable Webhook Alerting (ServerChan and Bark)

To deliver real-time trade updates and daily equity reports without heavy messaging infrastructure, we implement a pluggable notifier adapter targeting ServerChan (WeChat templates) with drop-in support for Bark (iOS native push). Notifications are dispatched asynchronously via background worker threads so HTTP webhook network latency never impacts the critical path of trading execution.
