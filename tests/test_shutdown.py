# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2019 EdNoepel
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import pytest
from datetime import datetime, timedelta
import time

from pyflex import Address
from pyflex.approval import directly
from pyflex.deployment import GfDeployment
from pyflex.gf import Collateral
from pyflex.numeric import Wad, Ray, Rad
from pyflex.shutdown import ESM, GlobalSettlement
from pyflex.auctions import SettlementSurplusAuctioneer
from pyflex.auctions import PostSettlementSurplusAuctionHouse

from tests.helpers import time_travel_by
from tests.test_auctions import create_surplus
from tests.test_gf import mint_prot, wait, wrap_eth, wrap_modify_cdp_collateralization

def open_cdp(geb: GfDeployment, collateral: Collateral, address: Address):
    assert isinstance(geb, GfDeployment)
    assert isinstance(collateral, Collateral)
    assert isinstance(address, Address)

    collateral.approve(address)
    wrap_eth(geb, address, Wad.from_number(10))
    assert collateral.adapter.join(address, Wad.from_number(10)).transact(from_address=address)
    wrap_modify_cdp_collateralization(geb, collateral, address, Wad.from_number(10), Wad.from_number(15))

    assert geb.cdp_engine.global_debt() >= Rad(Wad.from_number(15))
    assert geb.cdp_engine.coin_balance(address) >= Rad.from_number(10)

def create_surplus_auction(geb: GfDeployment, deployment_address: Address, our_address: Address):
    assert isinstance(geb, GfDeployment)
    assert isinstance(deployment_address, Address)
    assert isinstance(our_address, Address)

    surplus_auction_house = geb.surplus_auction_house
    create_surplus(geb, surplus_auction_house, deployment_address)
    coin_balance = geb.cdp_engine.coin_balance(geb.accounting_engine.address)
    assert coin_balance > geb.cdp_engine.debt_balance(geb.accounting_engine.address) + geb.accounting_engine.surplus_auction_amount_to_sell() + geb.accounting_engine.surplus_buffer()
    assert (geb.cdp_engine.debt_balance(geb.accounting_engine.address) - geb.accounting_engine.debt_queue()) - geb.accounting_engine.total_on_auction_debt() == Rad(0)
    assert geb.accounting_engine.auction_surplus().transact()

    mint_prot(geb.prot, our_address, Wad.from_number(10))
    surplus_auction_house.approve(geb.prot.address, directly(from_address=our_address))
    bid = Wad.from_number(0.001)
    assert geb.prot.balance_of(our_address) > bid
    assert surplus_auction_house.increase_bid_size(surplus_auction_house.auctions_started(), geb.accounting_engine.surplus_auction_amount_to_sell(), bid).transact(from_address=our_address)


nobody = Address("0x0000000000000000000000000000000000000000")
#@pytest.mark.skip(reason="tmp")
class TestESM:
    """This test must be run after other GEB tests because it will leave the testchain `disabled`d."""

    def test_init(self, geb, deployment_address, our_address):
        assert geb.esm is not None
        assert isinstance(geb.esm, ESM)
        assert isinstance(geb.esm.address, Address)
        assert geb.esm.trigger_threshold() > Wad(0)
        assert not geb.esm.settled()

        coin_balance = geb.cdp_engine.coin_balance(geb.accounting_engine.address)
        awe = geb.cdp_engine.debt_balance(geb.accounting_engine.address)
        # If `test_shutdown.py` is run in isolation, create a surplus auction to exercise `terminate_auction_prematurely`
        if coin_balance == Rad(0) and awe == Rad(0):
            create_surplus_auction(geb, deployment_address, our_address)

    def test_shutdown(self, geb, our_address, deployment_address):
        open_cdp(geb, geb.collaterals['ETH-A'], our_address)

        mint_prot(geb.prot, deployment_address, geb.esm.trigger_threshold())

        assert not geb.esm.settled()

        assert geb.prot.balance_of(deployment_address) >= geb.esm.trigger_threshold()
        assert geb.prot.approve(geb.esm.address).transact(from_address=deployment_address)
        assert geb.prot.allowance_of(deployment_address, geb.esm.address) >= geb.esm.trigger_threshold()

        assert geb.prot.address == Address(geb.esm._contract.functions.protocolToken().call())

        assert geb.global_settlement.contract_enabled()
        assert geb.cdp_engine.contract_enabled()
        assert geb.liquidation_engine.contract_enabled()
        assert geb.accounting_engine.contract_enabled()

        assert geb.esm.authorized_accounts(deployment_address) == True
        assert geb.global_settlement.authorized_accounts(geb.esm.address) == True
        assert geb.cdp_engine.authorized_accounts(geb.global_settlement.address) == True
        assert geb.liquidation_engine.authorized_accounts(geb.global_settlement.address) == True
        assert geb.accounting_engine.authorized_accounts(geb.global_settlement.address) == True
        assert geb.oracle_relayer.authorized_accounts(geb.global_settlement.address) == True

        assert geb.esm.shutdown().transact(from_address=deployment_address)

        assert geb.esm.settled()
        assert not geb.global_settlement.contract_enabled()
        assert not geb.cdp_engine.contract_enabled()
        assert not geb.liquidation_engine.contract_enabled()
        assert not geb.accounting_engine.contract_enabled()
        assert not geb.oracle_relayer.contract_enabled()

#@pytest.mark.skip(reason="tmp")
class TestGlobalSettlement:
    """This test must be run after TestESM, which calls `esm.shutdown`."""

    def test_init(self, geb):
        assert geb.global_settlement is not None
        assert isinstance(geb.global_settlement, GlobalSettlement)
        assert isinstance(geb.esm.address, Address)

    def test_getters(self, geb):
        assert not geb.global_settlement.contract_enabled()
        assert datetime.utcnow() - timedelta(minutes=5) < geb.global_settlement.shutdown_time() < datetime.utcnow()
        assert geb.global_settlement.shutdown_cooldown() >= 0
        assert geb.global_settlement.outstanding_coin_supply() >= Rad(0)

        for collateral in geb.collaterals.values():
            collateral_type = collateral.collateral_type
            assert geb.global_settlement.final_coin_per_collateral_price(collateral_type) == Ray(0)
            assert geb.global_settlement.collateral_shortfall(collateral_type) == Wad(0)
            assert geb.global_settlement.collateral_total_debt(collateral_type) == Wad(0)
            assert geb.global_settlement.collateral_cash_price(collateral_type) == Ray(0)

    def test_freeze_collateral_type(self, geb):
        collateral_type = geb.collaterals['ETH-A'].collateral_type

        assert geb.global_settlement.freeze_collateral_type(collateral_type).transact()

        assert geb.global_settlement.collateral_total_debt(collateral_type) > Wad(0)
        assert geb.global_settlement.final_coin_per_collateral_price(collateral_type) > Ray(0)

    def test_terminate_auction_prematurely(self, geb):
        last_surplus_auction = geb.surplus_auction_house.bids(geb.surplus_auction_house.auctions_started())
        last_collateral_auction = geb.debt_auction_house.bids(geb.debt_auction_house.auctions_started())
        if last_surplus_auction.auction_deadline > 0 and last_surplus_auction.high_bidder is not nobody:
            auction = geb.surplus_auction_house
        elif last_collateral_auction.auction_deadline > 0 and last_collatera_auction.high_bidder is not nobody:
            auction = geb.debt_auction_house
        else:
            auction = None

        if auction:
            print(f"active {auction} auction: {auction.bids(auction.auctions_started())}")
            assert not auction.contract_enabled()
            auction_id = auction.auctions_started()
            assert auction.terminate_auction_prematurely(auction_id).transact()
            assert auction.bids(auction_id).high_bidder == nobody

    def test_process_cdp(self, geb, our_address):
        collateral_type = geb.collaterals['ETH-A'].collateral_type

        cdp = geb.cdp_engine.cdp(collateral_type, our_address)
        assert cdp.generated_debt > Wad(0)
        assert geb.cdp_engine.collateral_type(collateral_type.name).accumulated_rates > Ray(0)
        assert geb.global_settlement.final_coin_per_collateral_price(collateral_type) > Ray(0)

        owe = Ray(cdp.generated_debt) * geb.cdp_engine.collateral_type(collateral_type.name).accumulated_rates * geb.global_settlement.final_coin_per_collateral_price(collateral_type)

        assert owe > Ray(0)
        wad = min(Ray(cdp.locked_collateral), owe)
        print(f"owe={owe} wad={wad}")

        assert geb.global_settlement.process_cdp(collateral_type, our_address).transact()
        assert geb.cdp_engine.cdp(collateral_type, our_address).generated_debt == Wad(0)
        assert geb.cdp_engine.cdp(collateral_type, our_address).locked_collateral > Wad(0)
        assert geb.cdp_engine.debt_balance(geb.accounting_engine.address) > Rad(0)

        assert geb.cdp_engine.global_debt() > Rad(0)
        assert geb.cdp_engine.global_unbacked_debt() > Rad(0)

    def test_close_cdp(self, web3, geb, our_address):
        collateral = geb.collaterals['ETH-A']
        collateral_type = collateral.collateral_type

        assert geb.global_settlement.free_collateral(collateral_type).transact()
        assert geb.cdp_engine.cdp(collateral_type, our_address).locked_collateral == Wad(0)
        assert geb.cdp_engine.token_collateral(collateral_type, our_address) > Wad(0)
        assert collateral.adapter.exit(our_address, geb.cdp_engine.token_collateral(collateral_type, our_address)).transact()

        assert geb.global_settlement.shutdown_cooldown() == 0
        time_travel_by(web3, 5)
        assert geb.global_settlement.set_outstanding_coin_supply().transact()
        assert geb.global_settlement.calculate_cash_price(collateral_type).transact()
        assert geb.global_settlement.collateral_cash_price(collateral_type) > Ray(0)

    @pytest.mark.skip(reason="unable to add system_coin to the `coin_bag`")
    def test_prepare_coins_for_redeeming(self, geb, our_address):
        assert geb.global_settlement.coin_bag(our_address) == Wad(0)
        assert geb.global_settlement.outstanding_coin_supply() > Rad(0)
        assert geb.system_coin.approve(geb.global_settlement.address).transact()
        assert geb.cdp_engine.coin_balance(our_address) >= Rad.from_number(10)
        # FIXME: `prepareCoinsForRedeeming` fails, possibly because we're passing 0 to `cdpEngine.transfer_collateral`
        assert geb.global_settlement.prepare_coins_for_redeeming(Wad.from_number(10)).transact()
        assert geb.global_settlement.coin_bag(our_address) == Wad.from_number(10)

class TestPostSettlementSurplusAuctioneer:
    @pytest.fixture(scope="session")
    def surplus_auctioneer(self, geb: GfDeployment) -> SettlementSurplusAuctioneer:
        return geb.surplus_auctioneer

    def test_getters(self, geb, surplus_auctioneer):
        assert surplus_auctioneer.accounting_engine() == geb.accounting_engine.address
        assert surplus_auctioneer.address == geb.accounting_engine.post_settlement_surplus_drain()
        assert surplus_auctioneer.cdp_engine() == geb.cdp_engine.address
        assert surplus_auctioneer.last_surplus_auction_time() == 0

    def _test_post_settlement_surplus_auction(self, web3, geb, our_address, surplus_auctioneer):
        assert geb.accounting_engine.contract_enabled() == False
        prev_coin_balance = geb.cdp_engine.coin_balance(surplus_auctioneer.address)
        surplus_auction_house = PostSettlementSurplusAuctionHouse(web3, surplus_auctioneer.surplus_auction_house())
        assert len(surplus_auction_house.active_auctions()) == 0

        # Start first auction
        assert surplus_auctioneer.auction_surplus().transact()
        assert len(surplus_auction_house.active_auctions()) == 1

        # Check bid and auctioneer coin balance
        bid = surplus_auction_house.bids(1)
        after_coin_balance = geb.cdp_engine.coin_balance(surplus_auctioneer.address)
        assert bid.id == 1
        assert bid.bid_amount == Wad(0)
        assert bid.high_bidder == Address(surplus_auctioneer.address)
        assert(prev_coin_balance - after_coin_balance == bid.amount_to_sell)

        #shouldn't be able to start another auction 
        while datetime.utcnow() - timedelta(seconds=geb.accounting_engine.surplus_auction_delay) > datatime.utcnow():
            assert surplus_auctioneer.auction_surplus().transact() == None
            time.sleep(2)

        # Start second auction
        assert surplus_auctioneer.auction_surplus().transact()

        assert len(surplus_auction_house.active_auctions()) == 2

        # Check bid and auctioneer coin balance
        bid = surplus_auction_house.bids(2)
        after_after_coin_balance = geb.cdp_engine.coin_balance(surplus_auctioneer.address)
        assert bid.id == 1
        assert bid.bid_amount == Wad(0)
        assert bid.high_bidder == Address(surplus_auctioneer.address)
        assert(after_coin_balance - after_after_coin_balance == bid.amount_to_sell)

    def test_post_settlement_surplus_auction_no_other_bids(self, web3, geb, our_address, surplus_auctioneer):
        assert geb.accounting_engine.contract_enabled() == False
        prev_coin_balance = geb.cdp_engine.coin_balance(surplus_auctioneer.address)
        surplus_auction_house = PostSettlementSurplusAuctionHouse(web3, surplus_auctioneer.surplus_auction_house())
        assert len(surplus_auction_house.active_auctions()) == 0

        # Start first auction
        assert surplus_auctioneer.auction_surplus().transact()
        assert len(surplus_auction_house.active_auctions()) == 1
        bid = surplus_auction_house.bids(1)
        print(bid)

        # Wait for bid duration
        wait(geb, our_address, surplus_auction_house.bid_duration()+1)

        # Can't settle with no other bids
        assert surplus_auction_house.settle_auction(1).transact() == None


        

