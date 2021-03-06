# Program name: atomicpp/Prad.py
# Author: Thomas Body
# Author email: tajb500@york.ac.uk
# Date of creation: 11 August 2017
#
# Use the atomic++ module to evaluate the rate-coefficients from OpenADAS

import numpy as np
from atomicpp import atomicpy
from scipy.integrate import odeint #ODEPACK, for numerical integration
# from scipy.integrate import simps #Simpson's rule, for definite integrals
import pickle
import matplotlib.pyplot as plt
import random
random.seed(1) #To ensure results are reproducible

# Code to hide OPEPACK/lsoda warnings (stdout)
# From https://stackoverflow.com/questions/31681946/disable-warnings-originating-from-scipy
import os
import sys
import contextlib

def fileno(file_or_fd):
    fd = getattr(file_or_fd, 'fileno', lambda: file_or_fd)()
    if not isinstance(fd, int):
        raise ValueError("Expected a file (`.fileno()`) or a file descriptor")
    return fd

@contextlib.contextmanager
def stdout_redirected(to=os.devnull, stdout=None):
    """
    https://stackoverflow.com/a/22434262/190597 (J.F. Sebastian)
    """
    if stdout is None:
       stdout = sys.stdout

    stdout_fd = fileno(stdout)
    # copy stdout_fd before it is overwritten
    #NOTE: `copied` is inheritable on Windows when duplicating a standard stream
    with os.fdopen(os.dup(stdout_fd), 'wb') as copied: 
        stdout.flush()  # flush library buffers that dup2 knows nothing about
        try:
            os.dup2(fileno(to), stdout_fd)  # $ exec >&to
        except ValueError:  # filename
            with open(to, 'wb') as to_file:
                os.dup2(to_file.fileno(), stdout_fd)  # $ exec > to
        try:
            yield stdout # allow code to be run with the redirected stdout
        finally:
            # restore stdout to its previous value
            #NOTE: dup2 makes stdout_fd inheritable unconditionally
            stdout.flush()
            os.dup2(copied.fileno(), stdout_fd)  # $ exec >&copied

# OOP method for solving the differential equations, to allow for additional output
class AtomicSolver(object):

    def __init__(self, impurity_symbol):
        
        # ImpuritySpecies
        self.impurity_symbol = impurity_symbol
        self.impurity = atomicpy.PyImpuritySpecies(impurity_symbol)

        # Evaluation parameters
        self.t_values = np.logspace(-6, 2, 200)
        self.Te_values = np.logspace(-0.69, 3.99, 100) #eV, span the entire array for which there is ADAS data
        self.Te_const = 50
        self.Ne_values = np.logspace(13.7, 21.3, 100) #m^-3
        self.Ne_const = [1e16,1e17,1e18,1e19,1e20,1e21]
        self.Ne_tau_values = [1e21, 1e19, 1e17, 1e15] #m^-3 s, values to return Prad(tau) for

        # RateEquations
        self.impurity_derivatives = atomicpy.PyRateEquations(self.impurity)
        self.impurity_derivatives.setThresholdDensity(-1.0) #Don't use a threshold density at first
        self.impurity_derivatives.setDominantIonMass(1.0)

        # Initial values
        self.Z = self.impurity.get_atomic_number()
        self.Te  = 1.294 #eV
        self.Ne  = 1e14 #m^-3
        self.Vi  = 0 #m/s
        self.Nn  = 0 #m^-3
        self.Vn  = 0 #m/s
        self.Nzk = np.zeros((self.Z+1,)) #m^-3
        self.Nzk[0] = 1e20 #m^-3 - start in g.s.
        self.Vzk = np.zeros((self.Z+1,)) #m/s

        # Additional output initialisation
        self.additional_out = {'Prad':[], 'Pcool':[], 'dNzk':[], 'F_zk':[], 'dNe':[], 'F_i':[], 'dNn':[], 'F_n':[]} #Blank lists to append onto
        self.additional_out_keys = ['Prad', 'dNzk'] #Keys to record data for
    
    @staticmethod
    def evolveDensity(Nzk, t, self, Te, Ne):
        # Te  = self.Te
        # Ne  = self.Ne
        Vi  = self.Vi
        Nn  = self.Nn
        Vn  = self.Vn
        # Nzk = self.Nzk
        Vzk = self.Vzk

        # Prevent negative densities
        # (these are possible if the time-step is large)
        for k in range(len(Nzk)):
            if(Nzk[k] < 0):
                Nzk[k] = 0.0

        derivative_struct = self.impurity_derivatives.computeDerivsHydrogen(Te, Ne, Nzk, Vzk);

        dNzk = derivative_struct["dNzk"]
        for key in self.additional_out_keys:
            self.additional_out[key].append(derivative_struct[key])

        return dNzk

    @staticmethod
    def evolveDensity_withRefuelling(Nzk, t, self, Te, Ne, refuelling_rate):
        # Te  = self.Te
        # Ne  = self.Ne
        Vi  = self.Vi
        Nn  = self.Nn
        Vn  = self.Vn
        # Nzk = self.Nzk
        Vzk = self.Vzk

        # Prevent negative densities
        # (these are possible if the time-step is large)
        for k in range(len(Nzk)):
            if(Nzk[k] < 0):
                Nzk[k] = 0.0

        derivative_struct = self.impurity_derivatives.computeDerivsHydrogen(Te, Ne, Nzk, Vzk);
        dNzk = derivative_struct["dNzk"]

        fraction_in_stage = Nzk/sum(Nzk)
        # Add neutrals at a rate of tau^-1
        dNzk[0] += sum(Nzk)*refuelling_rate
        # Remove other stages based on their density
        dNzk -= sum(Nzk)*refuelling_rate*fraction_in_stage

        for key in self.additional_out_keys:
            self.additional_out[key].append(derivative_struct[key])

        return dNzk

    def reset_additional_out(self):
        self.additional_out = {'Prad':[], 'Pcool':[], 'dNzk':[], 'F_zk':[], 'dNe':[], 'F_i':[], 'dNn':[], 'F_n':[]}

    def timeIntegrate(self, Te, Ne, refuelling_rate = 0):
        Vi  = self.Vi
        Nn  = self.Nn
        Vn  = self.Vn
        Vzk = self.Vzk

        print("Te = {:.2e}eV, Ne = {:.2e}/m3, tau_inv = {:.2e}".format(Te, Ne, refuelling_rate))
        
        if refuelling_rate == 0:
            # No refuelling - coronal equilibrium case
            with stdout_redirected():
                (result, output_dictionary) = odeint(self.evolveDensity, self.Nzk, self.t_values, args=(self, Te, Ne), printmessg=False, full_output=True, mxhnil=0)
        else:
            # Refuelling case
            with stdout_redirected():
                (result, output_dictionary) = odeint(self.evolveDensity_withRefuelling, self.Nzk, self.t_values, args=(self, Te, Ne, refuelling_rate), printmessg=False, full_output=True, mxhnil=0)
            # Will change the result, but may be treated the same as the CR case

        feval_at_step = output_dictionary['nfe'] #function evaluations at the time-step
        time_at_step = output_dictionary['tcur'] #time at the time-step

        time_indices = np.searchsorted(self.t_values, time_at_step, side='left') #find how the time-steps are distributed. Usually close but not 1 to 1 with self.t_values

        for key, value in self.additional_out.items():
            if value: #if list isn't empty - i.e. additional output has been recorded for this key
                output_feval = value #copy the output evaluated at each time-step

                try:
                    # If the additional_out has a length (i.e. is an array)
                    output_values = np.zeros((len(self.t_values), len(output_feval[0]))) #Output values corresponding to the self.t_values

                    # Fill the first few values from the first function evaluation (corresponding to __init__)
                    output_values[0:time_indices[0]] = output_feval[0]

                    # Fill the rest of the values by matching the time of the time=step to a self.t_values time
                    for step in range(len(feval_at_step)-1):
                        # Might need one feval to span multiple self.t_values
                        output_values[time_indices[step]:time_indices[step+1]] = output_feval[feval_at_step[step]-1]

                    self.additional_out[key] = output_values #copy the adjusted array back onto the additional_out attribute

                except TypeError:
                    output_values = np.zeros(len(self.t_values)) #Output values corresponding to the self.t_values

                    # Fill the first few values from the first function evaluation (corresponding to __init__)
                    output_values[0:time_indices[0]] = output_feval[0]

                    # Fill the rest of the values by matching the time of the time=step to a self.t_values time
                    for step in range(len(feval_at_step)-1):
                        # Might need one feval to span multiple self.t_values
                        output_values[time_indices[step]:time_indices[step+1]] = output_feval[feval_at_step[step]-1]

                    self.additional_out[key] = output_values #copy the adjusted array back onto the additional_out attribute

            if refuelling_rate > 0: 
                self.Nzk = np.zeros((self.Z+1,)) #m^-3 Always need to start system is g.s. for Prad_tau calculation 
                self.Nzk[0] = 1e19 #m^-3 - start in g.s. 
            else: 
                # self.Nzk = result[-1,:] # Can use previous result to try speed up evaluation if not calculating Prad(tau)
                pass # However, this is found to result in odd numerical behaviour
                


        return result

    def scanTempCREquilibrium(self):

        additional_out = {}
        for key in self.additional_out.keys():
            additional_out[key] = []

        results = np.zeros((len(self.Te_values),self.Z+1))

        for Te_iterator in range(len(self.Te_values)):
            self.reset_additional_out()
            Te = self.Te_values[Te_iterator]

            print("Evaluating test {} of {}".format(Te_iterator, len(self.Te_values)))

            result = self.timeIntegrate(Te, self.Ne_const)

            results[Te_iterator,:] = result[-1,:]

            for key in self.additional_out_keys:
                additional_out[key].append(self.additional_out[key][-1]) #Take the last time slice

        self.additional_out = additional_out #Replace the additional_out with the end-values

        return results

    def scanTempRefuelling(self, Te_values, N_values, refuelling_rate=0):
        """
        Te_values  1D array of electron temperatures in eV
        N_values   1D array of density values in m^-3
        """
        
        additional_out = {}
        refuelling_out = {}

        for key in self.additional_out.keys():
            additional_out[key] = []
            refuelling_out[key] = []

        results = np.zeros((len(Te_values),len(N_values),self.Z+1))

        # Loop through each value of Te in Te_values
        for Te_index, Te in enumerate(Te_values):

            print("Evaluating test {} of {}".format(Te_index, len(Te_values)))
            
            for key in self.additional_out_keys:
                refuelling_out[key] = [] #Reset for each time slice

            # Loop through each density in N_values
            for N_index, N in enumerate(N_values):
                self.reset_additional_out()
                
                result = self.timeIntegrate(Te, N, refuelling_rate=refuelling_rate)
                
                results[Te_index, N_index, :] = result[-1,:]

                for key in self.additional_out_keys:
                    refuelling_out[key].append(self.additional_out[key][-1]) #Take the last time slice
            
            for key in self.additional_out_keys:
                additional_out[key].append(refuelling_out[key]) #Append the list of refuelling-specific values

        self.additional_out = additional_out #Replace the additional_out with the end-values

        return results

    def scanDensityCREquilibrium(self):
        self.reset_additional_out()

        results = np.zeros((self.Z+1, len(self.Ne_values)))

        for Ne_iterator in range(len(self.Ne_values)):
            Ne = self.Ne_values[Ne_iterator]

            print("Evaluating test {} of {}".format(Ne_iterator, len(self.Ne_values)))

            result = self.timeIntegrate(self.Te_const, Ne, self.t_values)

            results[:,Ne_iterator] = result[-1,:]

            self.Nzk = result[-1,:]

        return results

# End class methods

# Plotting methods
def plotResultFromDensityEvolution(solver, result, plot_power = False, x_axis_scale = "log", y_axis_scale = "linear", grid = "none", align_yticks = True, show=False):
    fig, ax1 = plt.subplots()
    for k in range(solver.Z+1):
        if k == 0:
            ax1.semilogx(solver.t_values, result[:,k], label="{}".format("g.s."))
        else:
            ax1.semilogx(solver.t_values, result[:,k], label="{}+".format(k))

    # ax1.semilogx(solver.t_values, np.sum(result[:,:],1), label="Total")
    ax1.set_ylim(0, 1e20)
    ax1.set_xlabel(r'Time (s)')
    ax1.set_ylabel(r'Density of stage ($m^{-3}$)')
    # plt.title('Time evolution of ionisation stages')
    ax1.tick_params('y', colors = 'b')
    # ax1.legend()

    ax1.set_xlim(min(solver.t_values), max(solver.t_values))

    ax1.grid(which=grid, axis='both')

    if plot_power:
        ax2 = ax1.twinx()
        scaled_power = np.array(solver.additional_out['Prad'])*1e-3
        ax2.semilogx(solver.t_values, scaled_power,'k-.',label=r'$P_{rad}$',linewidth=1)
        ax2.set_ylim(min(scaled_power), max(scaled_power))
        ax2.set_ylabel(r'$P_{rad}$ (W $m^{-3}$)')
        ax2.tick_params('y', colors='k')
        # ax2.legend(loc=0)

    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1+h2, l1+l2, loc=0)
    
    ax1.set_xscale(x_axis_scale)
    ax1.set_yscale(y_axis_scale)
    if plot_power:
        ax2.set_yscale(y_axis_scale)
        if align_yticks:
            ax2.set_yticks(np.linspace(ax2.get_yticks()[0],ax2.get_yticks()[-1],len(ax1.get_yticks())))

    if show:
        plt.show()
    return fig

def plotScanTempCR_Dens(solver, reevaluate_scan=False, plot_power = False, x_axis_scale = "log", y_axis_scale = "linear", grid = "none", align_yticks = True, show=False):
    
    if reevaluate_scan:

        scan_temp = solver.scanTempCREquilibrium()

        with open('python_results/scanTempCREquilibrium({}_at_{},res+{})-INTEG_results.pickle'.format(len(solver.Te_values),solver.Ne_const,len(solver.t_values)), 'wb') as handle:
            pickle.dump(scan_temp, handle, protocol=pickle.HIGHEST_PROTOCOL)
        with open('python_results/scanTempCREquilibrium({}_at_{},res+{})-ADDIT_results.pickle'.format(len(solver.Te_values),solver.Ne_const,len(solver.t_values)), 'wb') as handle:
            pickle.dump(solver.additional_out, handle, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        with open('python_results/scanTempCREquilibrium({}_at_{},res+{})-INTEG_results.pickle'.format(len(solver.Te_values),solver.Ne_const,len(solver.t_values)), 'rb') as handle:
            scan_temp = pickle.load(handle)
        with open('python_results/scanTempCREquilibrium({}_at_{},res+{})-ADDIT_results.pickle'.format(len(solver.Te_values),solver.Ne_const,len(solver.t_values)), 'rb') as handle:
            solver.additional_out = pickle.load(handle)

    fig, ax1 = plt.subplots()

    for k in range(solver.Z+1):
        if k == 0:
            ax1.semilogx(solver.Te_values, scan_temp[:,k], label="{}".format("g.s."))
        else:
            ax1.semilogx(solver.Te_values, scan_temp[:,k], label="{}+".format(k))

    # plt.semilogx(solver.Te_values, np.sum(scan_temp[:,:],1), label="Total")

    total_density = np.sum(scan_temp[-1,:],0)
    ax1.set_ylim([1e-3*total_density, total_density])
    ax1.set_xlabel(r'Plasma temperature (eV)')
    ax1.set_ylabel(r'Density of stage ($m^{-3}$)')
    ax1.tick_params('y', colors = 'b')

    ax1.set_xlim(min(solver.Te_values), max(solver.Te_values))

    ax1.grid(which=grid, axis='both')

    if plot_power:
        ax2 = ax1.twinx()
        scaled_power = np.array(solver.additional_out['Prad'])*1e-3
        ax2.semilogx(solver.Te_values, scaled_power,'k-.',label=r'$P_{rad}$',linewidth=1)
        ax2.set_ylabel(r'$P_{rad}$ (KW $m^{-3}$)')
        ax2.tick_params('y', colors='k')
        ax2.set_ylim(0,)

        h1, l1 = ax1.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax1.legend(h1+h2, l1+l2, loc=0)
    else:
        ax1.legend(loc=0)
    
    ax1.set_xscale(x_axis_scale)
    ax1.set_yscale(y_axis_scale)
    if plot_power:
        ax2.set_yscale(y_axis_scale)
        if align_yticks:
            ax2.set_yticks(np.linspace(ax2.get_yticks()[0],ax2.get_yticks()[-1],len(ax1.get_yticks())))

    if show:
        plt.show()
    return fig

def plotScanTempCR_Prad_tau(solver, Te_values, N_values, x_axis_scale = "log", y_axis_scale = "log", grid = "none", show=False, refuelling_rate=0):
    """
    Te_values  1D array of electron temperatures in eV
    N_values  1D array of densities in m^-3
    """
    from scipy.interpolate import interp1d
    
    #if reevaluate_scan:
    scan_temp_refuelling = solver.scanTempRefuelling(Te_values, N_values, refuelling_rate=refuelling_rate)
    
    # Plot the results for the specified ne_tau values
    fig, ax = plt.subplots()

    Prad = np.array(solver.additional_out['Prad'])

    # Loop backwards through the index
    for N_index in range(len(N_values)-1,-1,-1):
        N = N_values[N_index]
        
        # This is total ion + neutral density
        total_density = np.sum(scan_temp_refuelling[-1,N_index,:],0)
        
        # NOTE: N should be very close to total_density
        if abs(N - total_density)/N > 1e-5:
            raise ValueError("N = {0}, total_density={1}".format(N, total_density))

        # Electron density = ion density
        Ne = scan_temp_refuelling[-1,N_index,1]
        
        Unit_transfer_Post = 1e13 ## Transfer from Wm^3 to erg*cm^3*s^-1

        ax.semilogx(Te_values, Prad[:,N_index]/(total_density*Ne)*Unit_transfer_Post,'k-.',label="C.R.", linewidth=1)
        print('total_density={}'.format(total_density))
        
    ax.set_xlabel(r'Plasma temperature (eV)')
    plt.legend(loc=0)

    ax.grid(which=grid, axis='both')
    
    ax.set_xscale(x_axis_scale)
    ax.set_yscale(y_axis_scale)

    if show:
        plt.show()
    return fig

def plotTestTimeIntegrator(solver, reevaluate_scan=False, show=False):

    fig, (ax1, ax2) = plt.subplots(2, sharex=False)

    # Determine high-resolution comparison results to compare against
    t_values_hi_res = np.logspace(-10, 2, 10000)

    if reevaluate_scan:
        prev_t_values     = solver.t_values

        solver.t_values   = t_values_hi_res #Use high resolution t values for this evaluation
        error_analysis    = solver.timeIntegrate(solver.Te_const, solver.Ne_const, 0)
        comparison_values = error_analysis[-1,:]
        comparison_values = np.append(comparison_values,solver.additional_out['Prad'][-1])

        solver.t_values   = prev_t_values #Reset to original t values
        with open('python_results/error_analysis({},{})(res+{})-comparison_values.pickle'.format(solver.Te_const,solver.Ne_const,len(solver.t_values)), 'wb') as handle:
            pickle.dump(comparison_values, handle, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        with open('python_results/error_analysis({},{})(res+{})-comparison_values.pickle'.format(solver.Te_const,solver.Ne_const,len(solver.t_values)), 'rb') as handle:
            comparison_values = pickle.load(handle)

    # Test whether specified time resolution affects the result

    time_test_values = np.round(np.logspace(1, 3, 20))
    if reevaluate_scan:
        time_test_results = [];
        prev_t_values = solver.t_values
        for time_iterator in range(len(time_test_values)):
            solver.reset_additional_out()

            time_test                             = time_test_values[time_iterator]
            print("Evaluating for time-resolution = {}pts".format(time_test))
            solver.t_values                       = np.logspace(-6, 2, time_test)
            error_analysis                        = solver.timeIntegrate(solver.Te_const, solver.Ne_const, 0)
            test_values                           = error_analysis[-1,:]
            test_values                           = np.append(test_values,solver.additional_out['Prad'][-1])
            time_test_results.append(comparison_values-test_values)

        solver.t_values   = prev_t_values #Reset to original t values   
        with open('python_results/error_analysis({},{})(res+{})-time_test_results.pickle'.format(solver.Te_const,solver.Ne_const,len(time_test_values)), 'wb') as handle:
            pickle.dump(time_test_results, handle, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        with open('python_results/error_analysis({},{})(res+{})-time_test_results.pickle'.format(solver.Te_const,solver.Ne_const,len(time_test_values)), 'rb') as handle:
            time_test_results = pickle.load(handle)

    ax1.plot(time_test_values, time_test_results)
    ax1.set_xlabel("Specified time steps")
    
    # Test whether the specified start time affects the result

    shift_test_values = np.linspace(-10,4, num=100)
    original_test_length = len(shift_test_values)
    if reevaluate_scan: 
        prev_t_values      = solver.t_values
        shift_test_values  = shift_test_values.tolist()
        shift_test_results = [];
        failed_test_values = [];
        for shift_iterator in range(len(shift_test_values)):
            solver.reset_additional_out()

            shift_test = shift_test_values[shift_iterator]
            print("Evaluating for shift = {}".format(shift_test))
            try:
                solver.t_values = np.logspace(shift_test, 5, 200)
                error_analysis  = solver.timeIntegrate(solver.Te_const, solver.Ne_const, 0)
                test_values     = error_analysis[-1,:]
                test_values     = np.append(test_values,solver.additional_out['Prad'][-1])
                shift_test_results.append((comparison_values - test_values)/comparison_values)
            except:
                print("Evaluation failed for shift = {}".format(shift_test))
                failed_test_values.append(shift_test)

        for shift_test in failed_test_values:
            shift_test_values.remove(shift_test)

        shift_test_results         = np.absolute(shift_test_results)
        shift_test_values          = np.array(shift_test_values)
        start_times                = np.power(10*np.ones_like(shift_test_values), shift_test_values)

        shift_test_data            = {}
        shift_test_data['results'] = shift_test_results
        shift_test_data['times']   = start_times
        solver.t_values            = prev_t_values #Reset to original t values
        with open('python_results/error_analysis({},{})(res+{})-shift_test_data.pickle'.format(solver.Te_const,solver.Ne_const,original_test_length), 'wb') as handle:
            pickle.dump(shift_test_data, handle, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        with open('python_results/error_analysis({},{})(res+{})-shift_test_data.pickle'.format(solver.Te_const,solver.Ne_const,original_test_length), 'rb') as handle:
            shift_test_data = pickle.load(handle)

    for k in range(solver.Z+2):
        if k == 0:
            ax2.loglog(shift_test_data['times'], shift_test_data['results'][:,k], label="{}".format("g.s."))
        elif k == solver.Z+1:
            ax2.loglog(shift_test_data['times'], shift_test_data['results'][:,k], label="{}".format(r"$P_{rad}$"))
        else:
            ax2.loglog(shift_test_data['times'], shift_test_data['results'][:,k], label="{}+".format(k))
    
    ax2.set_xlabel('Start time for evaluation (s)')
    fig.text(0.04, 0.5, r'Relative deviation from expected answer ($\Delta x/x$)', va='center', rotation='vertical')
    
    # ax2.legend(loc=0)
    plt.subplots_adjust(hspace=0.3, left=0.15)

    if show:
        plt.show()
    return fig

def findStddev(solver, reevaluate_scan=False):
    
    # Determine high-resolution comparison results to compare against
    t_values_hi_res = np.logspace(-10, 2, 10000)

    if reevaluate_scan:
        prev_t_values     = solver.t_values

        solver.t_values   = t_values_hi_res #Use high resolution t values for this evaluation
        error_analysis    = solver.timeIntegrate(solver.Te_const, solver.Ne_const, 0)
        comparison_values = error_analysis[-1,:]
        comparison_values = np.append(comparison_values,solver.additional_out['Prad'][-1])

        solver.t_values   = prev_t_values #Reset to original t values
        with open('python_results/error_analysis({},{})(res+{})-comparison_values.pickle'.format(solver.Te_const,solver.Ne_const,len(solver.t_values)), 'wb') as handle:
            pickle.dump(comparison_values, handle, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        with open('python_results/error_analysis({},{})(res+{})-comparison_values.pickle'.format(solver.Te_const,solver.Ne_const,len(solver.t_values)), 'rb') as handle:
            comparison_values = pickle.load(handle)

    samples_per_point = 50
    if reevaluate_scan:
        random_results = []
        store_Nzk = solver.Nzk
        for iterator in range(samples_per_point):
            solver.reset_additional_out()
            Nzk = np.zeros((solver.Z+1,))
            for k in range(solver.Z+1):
                Nzk[k] = random.random()*(10**random.uniform(1, 17))
            solver.Nzk = 1e17*Nzk/sum(Nzk)

            solver.t_values = np.logspace(-6, 2, 200)
            random_init = solver.timeIntegrate(solver.Te_const, solver.Ne_const, 0)
            random_values = random_init[-1,:]
            random_values = np.append(random_values,solver.additional_out['Prad'][-1])
            random_results.append(random_values)
        
        random_results = np.array(random_results)
        solver.Nzk = store_Nzk
        with open('python_results/error_analysis({},{})(res+{})-random_results.pickle'.format(solver.Te_const,solver.Ne_const,len(solver.t_values)), 'wb') as handle:
            pickle.dump(random_results, handle, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        with open('python_results/error_analysis({},{})(res+{})-random_results.pickle'.format(solver.Te_const,solver.Ne_const,len(solver.t_values)), 'rb') as handle:
            random_results = pickle.load(handle)

    for k in range(solver.Z+2):
        mean = np.mean(random_results[:,k])
        stdev = np.std(random_results[:,k])
        stdev_norm = stdev/mean
        diff = (mean - comparison_values[k])/comparison_values[k]
        if k == 0:
            print("{:5} -> mean = {:.2e}, stdev = {:.2e}, stdev_norm = {:.2e}, mean_diff = {:.2e}".format("g.s.", mean, stdev, stdev_norm, diff))
        elif k == solver.Z+1:
            print("{:5} -> mean = {:.2e}, stdev = {:.2e}, stdev_norm = {:.2e}, mean_diff = {:.2e}".format("P_rad", mean, stdev, stdev_norm, diff))
        else:
            print("{:5} -> mean = {:.2e}, stdev = {:.2e}, stdev_norm = {:.2e}, mean_diff = {:.2e}".format(k, mean, stdev, stdev_norm, diff))

def plotErrorPropagation(solver, reevaluate_scan=False, show=False, plot='both', show_species=[]):

    stdev_Te = np.linspace(0,solver.Te_const/2,num=20)
    stdev_Ne = np.linspace(0,solver.Ne_const/2,num=20)
    samples_per_point = 50
    if reevaluate_scan:
        solver.t_values = np.logspace(-6, 2, 200)

        if plot in ['Te','both']:
            stdev_norm_Te = []
            for sigma in stdev_Te:
                random_results = []
                for iterator in range(samples_per_point):
                    try:
                        solver.reset_additional_out()
                        Te = random.normalvariate(solver.Te_const, sigma)
                        Nzk = np.zeros((solver.Z+1,))
                        for k in range(solver.Z+1):
                            Nzk[k] = random.random()*(10**random.uniform(1, 17))
                        solver.Nzk = 1e17*Nzk/sum(Nzk)

                        random_te = solver.timeIntegrate(Te, solver.Ne_const, 0)
                        random_values = random_te[-1,:]
                        random_values = np.append(random_values,solver.additional_out['Prad'][-1])
                        if not(np.isnan(random_values).any()):
                            random_results.append(random_values)
                        else:
                            print("NaN for Te = {}".format(Te))
                    except:
                        print("Error for Te = {}".format(Te))

                mean = np.mean(random_results,0)
                stdev = np.std(random_results,0)
                stdev_norm_Te.append(np.absolute(stdev/mean))

            stdev_norm_Te = np.array(stdev_norm_Te)
            with open('python_results/error_analysis({},{})(res+{})-stdev_norm_Te.pickle'.format(solver.Te_const,solver.Ne_const,len(stdev_Te)), 'wb') as handle:
                pickle.dump(stdev_norm_Te, handle, protocol=pickle.HIGHEST_PROTOCOL)

        if plot in ['Ne','both']:
            stdev_norm_Ne = []
            for sigma in stdev_Ne:
                random_results = []
                for iterator in range(samples_per_point):
                    try:
                        solver.reset_additional_out()
                        Ne = random.normalvariate(solver.Ne_const, sigma)
                        Nzk = np.zeros((solver.Z+1,))
                        for k in range(solver.Z+1):
                            Nzk[k] = random.random()*(10**random.uniform(1, 17))
                        solver.Nzk = 1e17*Nzk/sum(Nzk)

                        random_te = solver.timeIntegrate(solver.Te_const, Ne, 0)
                        random_values = random_te[-1,:]
                        random_values = np.append(random_values,solver.additional_out['Prad'][-1])
                        if not(np.isnan(random_values).any()):
                            random_results.append(random_values)
                        else:
                            print("NaN for Ne = {}".format(Ne))
                    except:
                        print("Error for Ne = {}".format(Ne))

                mean = np.mean(random_results,0)
                stdev = np.std(random_results,0)
                stdev_norm_Ne.append(np.absolute(stdev/mean))

            stdev_norm_Ne = np.array(stdev_norm_Ne)
            with open('python_results/error_analysis({},{})(res+{})-stdev_norm_Ne.pickle'.format(solver.Te_const,solver.Ne_const,len(stdev_Ne)), 'wb') as handle:
                pickle.dump(stdev_norm_Ne, handle, protocol=pickle.HIGHEST_PROTOCOL)

    else:
        with open('python_results/error_analysis({},{})(res+{})-stdev_norm_Te.pickle'.format(solver.Te_const,solver.Ne_const,len(stdev_Te)), 'rb') as handle:
                stdev_norm_Te = pickle.load(handle)
        with open('python_results/error_analysis({},{})(res+{})-stdev_norm_Ne.pickle'.format(solver.Te_const,solver.Ne_const,len(stdev_Ne)), 'rb') as handle:
                stdev_norm_Ne = pickle.load(handle)

    if plot is 'Te':
        fig, ax = plt.subplots()    
        for k in range(solver.Z+2):
            if k == 0 and 0 in show_species:
                ax.plot(stdev_Te/solver.Te_const, stdev_norm_Te[:,k], label="{}".format("g.s."))
            elif k == solver.Z+1:
                ax.plot(stdev_Te/solver.Te_const, stdev_norm_Te[:,k], label="{}".format(r"$P_{rad}$"))
            else:
                if(k in show_species):
                    ax.plot(stdev_Te/solver.Te_const, stdev_norm_Te[:,k], label="{}+".format(k))
        ax.legend()
        ax.set_xlabel(r'Relative error in $T_e$')
        ax.set_ylabel(r'Relative error in parameter ($\sigma/\mu$)')
        vals = ax.get_yticks()
        ax.set_yticklabels(['{:3.0f}%'.format(x*100) for x in vals])
        ax.grid()
        ax.set_xlim(min(stdev_Te/solver.Te_const), max(stdev_Te/solver.Te_const))

        vals = ax.get_xticks()
        ax.set_xticklabels(['{:3.0f}%'.format(x*100) for x in vals])
        vals = ax.get_yticks()
        ax.set_yticklabels(['{:3.0f}%'.format(y*100) for y in vals])

    if plot is 'Ne':
        fig, ax = plt.subplots()    
        for k in range(solver.Z+2):
            if k == 0 and 0 in show_species:
                ax.plot(stdev_Ne/solver.Ne_const, stdev_norm_Ne[:,k], label="{}".format("g.s."))
            elif k == solver.Z+1:
                ax.plot(stdev_Ne/solver.Ne_const, stdev_norm_Ne[:,k], label="{}".format(r"$P_{rad}$"))
            else:
                if(k in show_species):
                    ax.plot(stdev_Ne/solver.Ne_const, stdev_norm_Ne[:,k], label="{}+".format(k))
        ax.legend()
        ax.set_xlabel(r'Relative error in $N_e$')
        ax.set_ylabel(r'Relative error in parameter ($\sigma/\mu$)')
        vals = ax.get_yticks()
        ax.set_yticklabels(['{:3.0f}%'.format(x*100) for x in vals])
        ax.grid()
        ax.set_xlim(min(stdev_Ne/solver.Ne_const), max(stdev_Ne/solver.Ne_const))

        vals = ax.get_xticks()
        ax.set_xticklabels(['{:3.0f}%'.format(x*100) for x in vals])
        vals = ax.get_yticks()
        ax.set_yticklabels(['{:3.0f}%'.format(y*100) for y in vals])


    if plot is 'both':
        fig, (ax1, ax2) = plt.subplots(2, sharex = False)

        for k in range(solver.Z+2):
            if k == 0 and 0 in show_species:
                ax1.plot(stdev_Te/solver.Te_const, stdev_norm_Te[:,k], label="{}".format("g.s."))
            elif k == solver.Z+1:
                ax1.plot(stdev_Te/solver.Te_const, stdev_norm_Te[:,k], 'k-.', label="{}".format(r"$P_{rad}$"), linewidth=1)
            else:
                if k in show_species:
                    ax1.plot(stdev_Te/solver.Te_const, stdev_norm_Te[:,k], label="{}+".format(k))
        ax1.legend()
        ax1.set_xlabel(r'Relative error in $T_e$')
        ax1.grid()

        for k in range(solver.Z+2):
            if k == 0 and 0 in show_species:
                ax2.plot(stdev_Ne/solver.Ne_const, stdev_norm_Ne[:,k], label="{}".format("g.s."))
            elif k == solver.Z+1:
                ax2.plot(stdev_Ne/solver.Ne_const, stdev_norm_Ne[:,k], 'k-.', label="{}".format(r"$P_{rad}$"), linewidth=1)
            else:
                if k in show_species:
                    ax2.plot(stdev_Ne/solver.Ne_const, stdev_norm_Ne[:,k], label="{}+".format(k))
        ax2.legend()
        ax2.set_xlabel(r'Relative error in $N_e$')
        ax2.grid()

        ax1.set_xlim(min(stdev_Te/solver.Te_const), max(stdev_Te/solver.Te_const))
        ax2.set_xlim(min(stdev_Ne/solver.Ne_const), max(stdev_Ne/solver.Ne_const))

        vals = ax1.get_xticks()
        ax1.set_xticklabels(['{:3.0f}%'.format(x*100) for x in vals])
        vals = ax2.get_xticks()
        ax2.set_xticklabels(['{:3.0f}%'.format(x*100) for x in vals])

        vals = ax1.get_yticks()
        ax1.set_yticklabels(['{:3.0f}%'.format(y*100) for y in vals])
        vals = ax2.get_yticks()
        ax2.set_yticklabels(['{:3.0f}%'.format(y*100) for y in vals])
        
        fig.text(0.04, 0.5, r'Relative error in parameter ($\sigma/\mu$)', va='center', rotation='vertical')

        plt.subplots_adjust(hspace=0.3, left=0.15)

    if show:
        plt.show()
    return fig

if __name__ == "__main__":

    # Control booleans
    reevaluate_scan           = False
    plot_solver_evolution     = False
    find_stddev               = False
    plot_test_time_integrator = False
    plot_error_propagation    = False
    plot_scan_temp_dens       = False
    plot_scan_temp_prad_tau   = True

    impurity_symbol = b'h' #need to include b (bytes) before the string for it to be sent as a std::string to C++

    solver = AtomicSolver(impurity_symbol)

    Te_values = np.logspace(-0.69, 3.99, 100)   # Electron temperature [eV]
    N_values = [1e16,1e17,1e18,1e19,1e20,1e21]  # Density of neutrals + ions [m^-3]
    
    path_to_output = 'Figures/'
    
    if plot_solver_evolution:
        solver_evolution = solver.timeIntegrate(solver.Te_const, solver.Ne_const, 0)
        plot_solver_evolution = plotResultFromDensityEvolution(solver, solver_evolution, plot_power = True, grid="major", show=False, y_axis_scale="linear")
        plot_solver_evolution.savefig(path_to_output+"solver_evolution.pdf")

    if find_stddev:
        findStddev(solver, reevaluate_scan = reevaluate_scan)

    if plot_test_time_integrator:
        plot_test_time_integrator = plotTestTimeIntegrator(solver, reevaluate_scan = reevaluate_scan)
        plot_test_time_integrator.savefig(path_to_output+"test_time_integrator.pdf")

    if plot_error_propagation:
        plot_error_propagation = plotErrorPropagation(solver, show_species=[4, 5], reevaluate_scan = reevaluate_scan)
        plot_error_propagation.savefig(path_to_output+"error_propagation.pdf")

    if plot_scan_temp_dens:
        plot_scan_temp_dens = plotScanTempCR_Dens(solver, grid="major", plot_power=True, reevaluate_scan = reevaluate_scan)
        plot_scan_temp_dens.savefig(path_to_output+"plot_scan_temp_dens.pdf")

    if plot_scan_temp_prad_tau:
        #if not(hydrogen_symbol is b'c'):
            #raise NotImplementedError('Prad_tau plot comparison data is for Carbon. Will need to add data for species {}'.format(str(hydrogen_symbol,'utf-8')))
        plot_scan_temp_prad_tau = plotScanTempCR_Prad_tau(solver, Te_values, N_values, grid="major")
        plot_scan_temp_prad_tau.savefig(path_to_output+"plot_scan_temp_prad_Hydrogen.pdf")

    plt.show()
    

























