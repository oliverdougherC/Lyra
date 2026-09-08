# Changelog

## [0.2.0-beta.1](https://github.com/oliverdougherC/Lyra/compare/v0.2.0-beta.0...v0.2.0-beta.1) (2026-09-08)


### Features

* make desktop beta delivery reproducible and recoverable ([68c0219](https://github.com/oliverdougherC/Lyra/commit/68c02194aec279656eac172a56643d749dfa6ea3))


### Bug Fixes

* preserve literal reasoning tags, including empty explicit reasoning channels; retain streamed text and reject oversized upload bodies before multipart parsing ([#89](https://github.com/oliverdougherC/Lyra/pull/89))
* recover failed draft editors, stabilize math styling across navigation, and bound source-preview reads ([#90](https://github.com/oliverdougherC/Lyra/pull/90))
* avoid false credit for very small numeric answers, send ambiguous prose-set answers to semantic grading, preserve uncertain-grade retries, target exact attention items, stabilize streamed answers, and batch class deck counts ([#91](https://github.com/oliverdougherC/Lyra/pull/91))
* surface durable draft-length warnings and improve Guide feedback prompts; writer length adherence and Guide semantic quality remain unresolved ([#92](https://github.com/oliverdougherC/Lyra/pull/92))

* allow hardened releases without an Apple developer account ([d664978](https://github.com/oliverdougherC/Lyra/commit/d6649782cf3d73dee2bfab9f75be13787800ef0a))
* honor explicit limits on tutor follow-up questions ([56830f6](https://github.com/oliverdougherC/Lyra/commit/56830f6c11893ca9e3a1ebadb45a7244a47756bd))
* keep beta study journeys readable and recoverable ([#82](https://github.com/oliverdougherC/Lyra/issues/82)) ([b1908fa](https://github.com/oliverdougherC/Lyra/commit/b1908faa69b46408e0190aed3700af474e7b80cc))
* keep follow-up drafts safe while Lyra responds ([e96bf48](https://github.com/oliverdougherC/Lyra/commit/e96bf4886977c648f9e7905c7807c806b1ae7a80))
* keep restored student data usable in the frozen application ([2ab548f](https://github.com/oliverdougherC/Lyra/commit/2ab548fc9c91eb50307aa0ee01b7dae4464ffd8e))
* keep the native restore confirmation attached to its app ([54e9ee5](https://github.com/oliverdougherC/Lyra/commit/54e9ee5fb6cbcf14b6576fac19e26f47646418b3))
* keep writer assessments finite without losing student prose ([#87](https://github.com/oliverdougherC/Lyra/issues/87)) ([3a58a7e](https://github.com/oliverdougherC/Lyra/commit/3a58a7ec34c778f844d485c1b3ab9ecc1a82b6f7))
* let completed native cleanup exit and make review candidates retrievable ([7540962](https://github.com/oliverdougherC/Lyra/commit/7540962b11810a35d90c9b0aceb51bf71701f3ef))
* make contributor onboarding and beta maintenance trustworthy ([56e77a9](https://github.com/oliverdougherC/Lyra/commit/56e77a979289db3bb190508b06b4f0e43593181b))
* make integrated local review delivery safe and traceable ([#76](https://github.com/oliverdougherC/Lyra/issues/76)) ([2918598](https://github.com/oliverdougherC/Lyra/commit/2918598de2aadf47f2456d8605f0bdd07e824ebd))
* make learning feedback and practice address the actual task ([#80](https://github.com/oliverdougherC/Lyra/issues/80)) ([ac75e0d](https://github.com/oliverdougherC/Lyra/commit/ac75e0d84901a77a064760432e3e942580d10080))
* make learning verification and card schemas truthful ([#85](https://github.com/oliverdougherC/Lyra/issues/85)) ([8aff363](https://github.com/oliverdougherC/Lyra/commit/8aff3633712ce277b9774eda644a6524f1273ba0))
* match guide responses to the amount of help requested ([8126ebb](https://github.com/oliverdougherC/Lyra/commit/8126ebbafa824671eed11817ca8424c709120d62))
* preserve current credential authority when imported data becomes active ([02be6a6](https://github.com/oliverdougherC/Lyra/commit/02be6a6abcea5a324ef1748fcdc95787c9b3db26))
* preserve follow-up messages and isolate acceptance credentials ([51fafab](https://github.com/oliverdougherC/Lyra/commit/51fafabf15a03071c896cc7a43f0e4086deaa0b5))
* preserve student work across cancellation and changing trust boundaries ([19c9e9e](https://github.com/oliverdougherC/Lyra/commit/19c9e9e824139ff682fb784c9fc0866f61a56421))
* preserve supported facts and student wording in corrections ([#86](https://github.com/oliverdougherC/Lyra/issues/86)) ([5df914a](https://github.com/oliverdougherC/Lyra/commit/5df914afa3b8ba03569383e06f90ed4f095d205a))
* preserve writer evidence and student edits across recovery ([#83](https://github.com/oliverdougherC/Lyra/issues/83)) ([dc3be3a](https://github.com/oliverdougherC/Lyra/commit/dc3be3acb7f3aee33dd6da328caf578b79ee4f8f))
* prevent packaged calculations from reentering app startup ([#88](https://github.com/oliverdougherC/Lyra/issues/88)) ([07ad451](https://github.com/oliverdougherC/Lyra/commit/07ad4511ea8eed287fd950dfadccf43af775827a))
* prevent quit from interrupting application replacement ([532845c](https://github.com/oliverdougherC/Lyra/commit/532845c32ca03d14036ed42327a0643501b5e782))
* protect helper leases through eviction, quit, and recovery ([#81](https://github.com/oliverdougherC/Lyra/issues/81)) ([6fb0570](https://github.com/oliverdougherC/Lyra/commit/6fb057082600729ebadcc3584a289e71c6fcef99))
* require the updater to initialize and accept its real archive ([dfa8425](https://github.com/oliverdougherC/Lyra/commit/dfa842528ebbbef8ef3d60a9f5bbe8b5290a7318))
* restore focus only after the composer is ready ([c0d9910](https://github.com/oliverdougherC/Lyra/commit/c0d991091d6bf9ea305e21ddff3ed67a8089690a))
* retain pending credential reads instead of declaring healthy keys missing ([b23ff5e](https://github.com/oliverdougherC/Lyra/commit/b23ff5e62e7a2e6493f122fb37ae75e090ceb593))
