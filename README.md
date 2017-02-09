HCF (HubStorage Frontier) Backend for Frontera
==============================================

This package contains a common Hubstorage backend, queue and states components for Frontera. 

Main features
-------------
 - States are implemented using collections.
 - Distributed Backend run mode isn't supported yet (but Distributed Spiders is).
 - Using memory cache to store states.
 - HCFQueue and HCFStates classes can be used as building blocks to arrange any kind of crawling logic.

Available Settings
------------------

 - ```HCF_AUTH``` : Scrapy Cloud API Key, required.
 - ```HCF_PROJECT_ID``` : Scrapy Cloud Project ID, required.
 - ```HCF_FRONTIER``` : Frontier name, required.
 - ```HCF_PRODUCER_BATCH_SIZE``` : Producer batch size, default is ```10000```.
 - ```HCF_PRODUCER_FLUSH_INTERVAL``` : Producer flush interval, default is ```30```.
 - ```HCF_PRODUCER_NUMBER_OF_SLOTS``` : Number of slots to use in the frontier, default is ```8```.
 - ```HCF_PRODUCER_SLOT_PREFIX``` : Frontier slot prefix, optional.
 - ```HCF_CLEANUP_ON_START``` : Whether to cleanup frontier on start.
 - ```HCF_CONSUMER_MAX_BATCHES``` : Maximum number of batches to consume, if
   not set consumes all batches.
 - ```HCF_CONSUMER_SLOT``` : Consumer frontier slot, default is ```0```.
